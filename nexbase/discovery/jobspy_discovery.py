"""JobSpy: the DIRECT handler for Indeed and LinkedIn.

Only those two are registered. Verified live on 2026-09-16 for a nationwide and
a state query; ZipRecruiter (403), Glassdoor (400) and Google Jobs (always empty)
failed and are no longer run through JobSpy. Freshness is pushed down to the
source via ``hours_old`` so stale postings are never even transferred.

Two things the previous implementation got wrong and this one does not:

* ``company_url`` from Indeed/LinkedIn is a **job-board profile URL**
  (``indeed.com/cmp/...``), not the employer's website. Only Indeed's
  ``company_url_direct`` is the real site, so the two are kept in separate
  fields and only the latter is trusted for domain derivation.
* JobSpy surfaces ``company_industry``, ``company_num_employees`` and
  ``emails`` for free. Those are exactly the facts the qualification gate needs
  in order to apply the employee-size and industry rules without paid credits,
  so they are carried through instead of discarded.
"""
from __future__ import annotations

import logging
import time
from itertools import product

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from nexbase.core.models import RawJob
from nexbase.core.source_tracking import JOB_BOARD, SourceInfo
from nexbase.core.timeutils import coerce_datetime
from nexbase.discovery.coverage import (
    BUDGET_REACHED,
    ERROR,
    SOURCE_EXHAUSTED,
    SourceOutcome,
    classify_source_log,
)
from nexbase.logging_setup import get_logger

#: Sites JobSpy 1.1.82 actually implements. Monster and SimplyHired are NOT
#: among them - they are served by `nexbase.discovery.board_scrapers`.
class _JobSpyLogCapture(logging.Handler):
    """Collects JobSpy's own WARNING/ERROR records for one site call.

    JobSpy reports a failed scrape by logging to ``JobSpy:<Site>`` and returning
    an empty frame. Those loggers set ``propagate = False``, so the records
    never reach the root logger and an outright failure is indistinguishable
    from a genuinely empty search unless we listen on them directly.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []
        self._attached: list[logging.Logger] = []

    def emit(self, record) -> None:
        try:
            self.messages.append(record.getMessage())
        except Exception:  # a broken log record must never break discovery
            pass

    def __enter__(self):
        # Every JobSpy logger that exists, plus the spellings its own
        # `scrape_jobs` derives lazily (indeed -> Indeed, zip_recruiter ->
        # ZipRecruiter). One site runs per call, so anything logged is its.
        names = {n for n in logging.root.manager.loggerDict if n.startswith("JobSpy:")}
        names |= {"JobSpy:Indeed", "JobSpy:LinkedIn", "JobSpy:Linkedin",
                  "JobSpy:ZipRecruiter", "JobSpy:Glassdoor", "JobSpy:Google"}
        for name in names:
            logger = logging.getLogger(name)
            logger.addHandler(self)
            self._attached.append(logger)
        return self

    def __exit__(self, *exc) -> bool:
        for logger in self._attached:
            logger.removeHandler(self)
        self._attached.clear()
        return False


#: Boards that serve a market NexBase does not operate in. Supported by the
#: package, never run by us: the brief is "USA-wide direct employers".
NON_US_SITES = frozenset({"bayt", "naukri", "bdjobs"})

SUPPORTED_SITES = frozenset(
    {"linkedin", "indeed", "zip_recruiter", "glassdoor", "google", "bayt", "naukri", "bdjobs"}
)

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _scrape_jobs(**kwargs):
    from jobspy import scrape_jobs

    return scrape_jobs(**kwargs)


def _clean(value):
    """Normalise pandas NaN / empty strings to None."""
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _split_emails(value) -> list[str]:
    value = _clean(value)
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        parts = str(value).split(",")
    return [p.strip().lower() for p in parts if p and "@" in str(p)]


class JobSpyDiscovery:
    """Discovers raw jobs from public job boards via JobSpy."""

    source: SourceInfo = JOB_BOARD

    def __init__(self, settings=None, logger=None) -> None:
        from nexbase.config import get_settings

        self.settings = settings or get_settings()
        self.log = logger or get_logger("nexbase.discovery.jobspy")

    # ------------------------------------------------------------------
    def search(
        self,
        search_terms: list[str],
        locations: list[str],
        site_names: list[str] | None = None,
        results_wanted: int | None = None,
        hours_old: int | None = None,
        country_indeed: str | None = None,
        client_industry: str | None = None,
        distance: int = 50,
        is_remote: bool = False,
        job_type: str | None = None,
        offset: int = 0,
        description_format: str = "markdown",
        linkedin_fetch_description: bool = True,
        enforce_annual_salary: bool = False,
        proxies: list[str] | None = None,
        user_agent: str | None = None,
        verbose: int = 0,
    ) -> list[RawJob]:
        """Run targeted JobSpy searches and return raw job records.

        All sites for a given (term, location) go in a **single** call so
        JobSpy's internal thread pool does the concurrency, rather than one
        serial call per site.
        """
        requested = site_names or []
        sites = [s for s in requested if s in SUPPORTED_SITES]
        unsupported = set(site_names or []) - SUPPORTED_SITES
        if unsupported:
            self.log.warning("jobspy_unsupported_sites", sites=sorted(unsupported))
        # NexBase is a US-only product, and JobSpy also ships bayt (Middle
        # East), naukri (India) and bdjobs (Bangladesh). A configuration slip
        # must not put a non-US board inside a US run.
        non_us = [s for s in sites if s in NON_US_SITES]
        if non_us:
            self.log.warning("jobspy_non_us_sites_refused", sites=sorted(non_us))
            sites = [s for s in sites if s not in NON_US_SITES]
        if not sites:
            self.log.error("jobspy_no_supported_sites")
            return []

        results_wanted = results_wanted or self.settings.jobspy_results_wanted
        country_indeed = country_indeed or self.settings.discovery_country

        records: list[RawJob] = []
        seen: set[tuple] = set()

        # One call per site. A single `scrape_jobs` call carrying every site
        # meant one broken source (ZipRecruiter 403, Glassdoor 400, Google
        # cursor) could raise and discard the Indeed and LinkedIn rows found in
        # the same call. Failures are now contained to the site that caused them
        # and reported per site rather than silently folded into a total.
        self.site_status: dict[str, dict] = {}
        self.last_outcomes: list[SourceOutcome] = []
        for term, location in product(search_terms, locations):
            for site in sites:
                started = time.monotonic()
                outcome = SourceOutcome(
                    source=f"jobspy:{site}", query=term, location_scope=location,
                    offset=offset, stop_reason=SOURCE_EXHAUSTED,
                )
                self.last_outcomes.append(outcome)
                self.log.info(
                    "jobspy_search_start",
                    term=term, location=location, site=site, hours_old=hours_old,
                )
                status = self.site_status.setdefault(
                    site, {"jobs": 0, "calls": 0, "errors": 0, "last_error": None}
                )
                status["calls"] += 1
                captured = _JobSpyLogCapture()
                try:
                    with captured:
                        df = _scrape_jobs(
                            site_name=[site],
                            search_term=term,
                            location=location,
                            distance=distance,
                            is_remote=is_remote,
                            job_type=job_type,
                            results_wanted=results_wanted,
                            hours_old=hours_old,
                            country_indeed=country_indeed,
                            offset=offset,
                            description_format=description_format,
                            linkedin_fetch_description=linkedin_fetch_description,
                            enforce_annual_salary=enforce_annual_salary,
                            proxies=proxies,
                            user_agent=user_agent,
                            verbose=verbose,
                        )
                except Exception as exc:
                    status["errors"] += 1
                    status["last_error"] = str(exc)
                    outcome.stop_reason = ERROR
                    outcome.error_type = type(exc).__name__
                    outcome.error_message = str(exc)
                    outcome.runtime_seconds = round(time.monotonic() - started, 2)
                    self.log.error(
                        "jobspy_search_error",
                        term=term, location=location, site=site, error=str(exc),
                    )
                    continue

                if df is None or df.empty:
                    reason, message = classify_source_log(captured.messages)
                    outcome.stop_reason = reason
                    outcome.error_message = message
                    outcome.runtime_seconds = round(time.monotonic() - started, 2)
                    if message:
                        # The source said why. Surface it instead of "empty".
                        status["errors"] += 1
                        status["last_error"] = message
                        self.log.warning(
                            "jobspy_search_failed", term=term, location=location,
                            site=site, stop_reason=reason, detail=message,
                        )
                    else:
                        self.log.info(
                            "jobspy_search_empty", term=term, location=location,
                            site=site,
                        )
                    continue

                outcome.jobs_returned = len(df)

                for row in df.to_dict("records"):
                    job = self._to_raw_job(row, client_industry=client_industry)
                    key = (job.source_site, job.external_id or job.application_url)
                    if key in seen:
                        outcome.duplicates += 1
                        continue
                    seen.add(key)
                    records.append(job)
                    outcome.jobs_accepted += 1
                    status["jobs"] += 1
                # JobSpy pages internally until it has `results_wanted` or the
                # board runs dry. Coming back full means the board still had
                # more and OUR number ended it; short means the board did.
                outcome.stop_reason = (
                    BUDGET_REACHED if outcome.jobs_returned >= results_wanted
                    else SOURCE_EXHAUSTED)
                outcome.runtime_seconds = round(time.monotonic() - started, 2)

                self.log.info(
                    "jobspy_search_complete",
                    term=term, location=location, site=site, rows=len(df),
                )

        self.log.info("jobspy_total", count=len(records), sites=self.site_status)
        return records

    # ------------------------------------------------------------------
    def _to_raw_job(self, row: dict, client_industry: str | None = None) -> RawJob:
        site = _clean(row.get("site"))
        job_url = _clean(row.get("job_url"))
        direct_url = _clean(row.get("job_url_direct")) or job_url

        # `company_url` is a board profile page on Indeed/LinkedIn.
        # `company_url_direct` (Indeed only) is the employer's real website.
        board_profile_url = _clean(row.get("company_url"))
        real_website = _clean(row.get("company_url_direct"))

        external_id = _clean(row.get("id")) or job_url

        return RawJob(
            source_type=self.source.source_type.value,
            source_priority=int(self.source.source_priority.value),
            source_site=site,
            external_id=str(external_id) if external_id is not None else None,
            title=_clean(row.get("title")),
            company_name=_clean(row.get("company")),
            company_url=board_profile_url,
            company_website=real_website,
            location=_clean(row.get("location")),
            employment_type=_clean(row.get("job_type")),
            description=_clean(row.get("description")),
            posted_at=coerce_datetime(row.get("date_posted")),
            application_url=direct_url,
            apply_url=job_url,
            ats_platform=None,
            # JobSpy emits no `country` column; location text carries it.
            country=None,
            is_remote=_clean(row.get("is_remote")),
            # Only what the board actually reported. The probe's industry is
            # search intent and is carried separately.
            company_industry=_clean(row.get("company_industry")),
            search_industry=client_industry,
            company_employee_count=_clean(row.get("company_num_employees")),
            company_revenue=_clean(row.get("company_revenue")),
            company_addresses=_clean(row.get("company_addresses")),
            applicant_count=None,  # JobSpy exposes none; see linkedin_signal.py
            observed_emails=_split_emails(row.get("emails")),
            raw={k: _clean(v) for k, v in row.items()},
        )
