"""Source registry: every portal NexBase searches, with exactly one handler each.

Three classes of source, as the Master Plan defines them:

* ``DIRECT`` - job boards. JobSpy handles Indeed, LinkedIn, ZipRecruiter,
  Glassdoor and Google; NexBase's own board adapters handle the rest.
* ``ATS`` - applicant tracking systems, one ats-scrapers slice per portal.
* ``APIFY`` - Apify actors. The class is supported; none is registered yet.

Every source carries its adapter, enabled/disabled state (``SOURCES_DISABLED``)
and rate limits (a minimum interval between calls and a per-run call budget).
Running a plan tracks errors and metrics per source and stamps provenance on
every job. A portal gets one handler: registering a second one raises.

Coverage: each title is searched nationwide first. A query that comes back
capped (by the source or by our own result budget) fans out to every state,
title by title, until the call budget is spent. A failing query never fans out.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

from nexbase.access.ratelimit import RateLimiter
from nexbase.config import Settings, get_settings
from nexbase.core.errors import DiscoveryError
from nexbase.core.models import RawJob
from nexbase.discovery.ats_discovery import ATSDiscovery, ATSSliceCache
from nexbase.discovery.board_scrapers import BOARD_SCRAPERS
from nexbase.discovery.coverage import (
    BLOCKED,
    BUDGET_REACHED,
    ERROR,
    RATE_LIMITED,
    ROBOTS_RESTRICTED,
    SOURCE_EXHAUSTED,
    SOURCE_LIMIT_REACHED,
    CoverageReport,
    SourceOutcome,
)
from nexbase.discovery.jobspy_discovery import JobSpyDiscovery
from nexbase.discovery.planner import COUNTRY, STATE, DiscoveryPlan, GeoCell, TitleVariant
from nexbase.logging_setup import get_logger


class SourceClass(str, Enum):
    DIRECT = "DIRECT"
    ATS = "ATS"
    APIFY = "APIFY"


#: Job boards JobSpy is the handler for.
JOBSPY_PORTALS = ("indeed", "linkedin", "zip_recruiter", "glassdoor", "google")
#: ats-scrapers slices registered as ATS sources.
ATS_PORTALS = (
    "greenhouse", "lever", "ashby", "workable", "smartrecruiters", "bamboohr",
    "breezy", "jazzhr", "recruitee", "paylocity",
)

#: The query had more results than it returned.
CAPPED = frozenset({BUDGET_REACHED, SOURCE_LIMIT_REACHED})
#: The query failed; the source is not asked for more of the same.
FAILED = frozenset({ERROR, BLOCKED, RATE_LIMITED, ROBOTS_RESTRICTED})


@dataclass(frozen=True)
class SourceQuery:
    title: TitleVariant
    geo: GeoCell
    sector: str
    hours_old: int


Adapter = Callable[[SourceQuery], "tuple[list[RawJob], SourceOutcome]"]


@dataclass
class Source:
    portal: str
    source_class: SourceClass
    handler: str
    adapter: Adapter
    enabled: bool = True
    min_interval_seconds: float = 0.0
    max_calls_per_run: int = 60

    @property
    def id(self) -> str:
        return f"{self.source_class.value.lower()}:{self.portal}"

    def describe(self) -> dict:
        return {
            "id": self.id, "portal": self.portal,
            "source_class": self.source_class.value, "handler": self.handler,
            "enabled": self.enabled,
            "min_interval_seconds": self.min_interval_seconds,
            "max_calls_per_run": self.max_calls_per_run,
        }


@dataclass
class SourceStats:
    """One source's metrics and errors for one run."""

    calls: int = 0
    jobs_returned: int = 0
    jobs_accepted: int = 0
    duplicates: int = 0
    errors: int = 0
    last_error: str | None = None
    fanouts: int = 0
    skipped_queries: int = 0
    duration_ms: int = 0
    stop_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def outcome(self) -> str:
        if self.jobs_accepted:
            return "SUCCESS"
        return "ERROR" if self.errors else "EMPTY"


@dataclass
class DiscoveryResult:
    jobs: list[RawJob]
    coverage: CoverageReport
    #: source id -> description, metrics and outcome (DISABLED when off).
    source_status: dict[str, dict]


class SourceRegistry:
    def __init__(self, logger=None) -> None:
        self.log = logger or get_logger("nexbase.discovery.registry")
        self._by_portal: dict[str, Source] = {}

    def register(self, source: Source) -> Source:
        existing = self._by_portal.get(source.portal)
        if existing is not None:
            raise DiscoveryError(
                f"portal {source.portal!r} already has a handler ({existing.id} via "
                f"{existing.handler}); a portal gets exactly one")
        self._by_portal[source.portal] = source
        return source

    def sources(self) -> list[Source]:
        return list(self._by_portal.values())

    def get(self, portal: str) -> Source | None:
        return self._by_portal.get(portal)

    # ------------------------------------------------------------------
    def run(self, plan: DiscoveryPlan, repo=None) -> DiscoveryResult:
        """Execute ``plan`` against every enabled source. One failure never
        stops the others."""
        result = DiscoveryResult(jobs=[], coverage=CoverageReport(), source_status={})
        for source in self.sources():
            if not source.enabled:
                result.source_status[source.id] = {**source.describe(), "outcome": "DISABLED"}
                continue
            stats = self._run_source(source, plan, repo, result)
            result.source_status[source.id] = {
                **source.describe(), **asdict(stats), "outcome": stats.outcome}
            self.log.info("source_complete", source=source.id, outcome=stats.outcome,
                          calls=stats.calls, accepted=stats.jobs_accepted,
                          errors=stats.errors, fanouts=stats.fanouts,
                          skipped_queries=stats.skipped_queries)
        return result

    def _run_source(self, source, plan, repo, result) -> SourceStats:
        stats = SourceStats()
        limiter = (RateLimiter(1.0 / source.min_interval_seconds)
                   if source.min_interval_seconds > 0 else None)
        queue = deque(SourceQuery(title, plan.geo_root, plan.sector, plan.hours_old)
                      for title in plan.titles)
        seen: set[str] = set()
        while queue:
            if stats.calls >= source.max_calls_per_run:
                stats.skipped_queries = len(queue)
                result.coverage.add(SourceOutcome(
                    source=source.id, stop_reason=BUDGET_REACHED,
                    error_message=(f"{len(queue)} planned queries not run: "
                                   f"max_calls_per_run={source.max_calls_per_run}")))
                self.log.info("source_budget_reached", source=source.id,
                              skipped_queries=len(queue))
                break
            query = queue.popleft()
            if limiter is not None:
                limiter.wait()
            outcome = self._call(source, query, plan, repo, stats, seen, result.jobs)
            result.coverage.add(outcome)
            if outcome.stop_reason in CAPPED and query.geo.level == COUNTRY:
                stats.fanouts += 1
                queue.extend(SourceQuery(query.title, geo, query.sector, query.hours_old)
                             for geo in plan.geo_partitions)
        return stats

    def _call(self, source, query, plan, repo, stats, seen, jobs) -> SourceOutcome:
        run_id = None
        if repo is not None:
            run_id = repo.insert_discovery_run({
                "run_key": plan.run_key, "source": source.id,
                "search_term": query.title.title, "location": query.geo.name,
                "client_industry": plan.sector, "status": "RUNNING",
                "params": {"handler": source.handler, "geo_code": query.geo.code,
                           "geo_level": query.geo.level,
                           "title_origin": query.title.origin,
                           "hours_old": query.hours_old},
            })

        stats.calls += 1
        started = time.monotonic()
        try:
            found, outcome = source.adapter(query)
        except Exception as exc:  # isolation: one broken call is one ERROR
            found, outcome = [], SourceOutcome(
                source=source.id, stop_reason=ERROR,
                error_type=type(exc).__name__, error_message=str(exc))
        outcome.source = source.id
        outcome.query = query.title.title
        outcome.location_scope = query.geo.name
        elapsed_ms = round((time.monotonic() - started) * 1000)
        stats.duration_ms += elapsed_ms

        fetched_at = datetime.now(timezone.utc).isoformat()
        accepted = 0
        for job in found:
            key = job.external_id or job.application_url
            if key and key in seen:
                outcome.duplicates += 1
                stats.duplicates += 1
                continue
            if key:
                seen.add(key)
            job.provenance = {
                "run_key": plan.run_key, "source_id": source.id,
                "source_class": source.source_class.value,
                "handler": source.handler, "portal": source.portal,
                "sector": plan.sector, "query_title": query.title.title,
                "title_origin": query.title.origin, "soc_code": query.title.soc_code,
                "geo_code": query.geo.code, "geo_level": query.geo.level,
                "fetched_at": fetched_at,
            }
            jobs.append(job)
            accepted += 1
        outcome.jobs_accepted = accepted
        stats.jobs_returned += outcome.jobs_returned
        stats.jobs_accepted += accepted
        stats.stop_reasons[outcome.stop_reason] = (
            stats.stop_reasons.get(outcome.stop_reason, 0) + 1)

        failed = outcome.stop_reason in FAILED
        if failed:
            stats.errors += 1
            stats.last_error = outcome.error_message or outcome.stop_reason
        (self.log.warning if failed else self.log.info)(
            "source_call", source=source.id, title=query.title.title,
            geo=query.geo.code, stop_reason=outcome.stop_reason, accepted=accepted,
            duration_ms=elapsed_ms, error=outcome.error_message if failed else None)

        if repo is not None and run_id is not None:
            repo.update_discovery_run(run_id, {
                "jobs_found": accepted,
                "status": "FAILED" if failed else "COMPLETE",
                "error": outcome.error_message if failed else None,
                "finished_at": fetched_at,
                "params": outcome.as_dict(),
            })
        return outcome


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------
def build_registry(settings: Settings | None = None, access=None, logger=None) -> SourceRegistry:
    """Every portal NexBase knows, wired to its handler, for one run."""
    settings = settings or get_settings()
    log = logger or get_logger("nexbase.discovery.registry")
    registry = SourceRegistry(log)
    disabled = settings.disabled_sources
    budget = settings.source_max_calls_per_run
    interval = settings.source_min_interval_seconds

    for portal in JOBSPY_PORTALS:
        registry.register(Source(
            portal, SourceClass.DIRECT, "jobspy", _jobspy_adapter(portal, settings, log),
            enabled=portal not in disabled, min_interval_seconds=interval,
            max_calls_per_run=budget))
    for portal, scraper in BOARD_SCRAPERS.items():
        registry.register(Source(
            portal, SourceClass.DIRECT, "nexbase-board", _board_adapter(scraper, access, log),
            enabled=portal not in disabled, min_interval_seconds=interval,
            max_calls_per_run=budget))
    # A slice is downloaded once per run, however many queries read it.
    slice_cache = ATSSliceCache(log)
    for portal in ATS_PORTALS:
        registry.register(Source(
            portal, SourceClass.ATS, "ats-scrapers",
            _ats_adapter(portal, settings, log, slice_cache),
            enabled=portal not in disabled, max_calls_per_run=budget))

    unknown = disabled - {s.portal for s in registry.sources()}
    if unknown:
        log.warning("sources_disabled_unknown", portals=sorted(unknown))
    return registry


def _jobspy_adapter(portal: str, settings, log) -> Adapter:
    def search(query: SourceQuery):
        jobspy = JobSpyDiscovery(settings, log)
        jobs = jobspy.search(
            search_terms=[query.title.title], locations=[query.geo.name],
            site_names=[portal], hours_old=query.hours_old,
            client_industry=query.sector)
        return jobs, jobspy.last_outcomes[-1]
    return search


def _board_adapter(scraper_class, access, log) -> Adapter:
    def search(query: SourceQuery):
        scraper = scraper_class(access=access, logger=log)
        jobs = scraper.search(
            search_terms=[query.title.title], locations=[query.geo.name],
            client_industry=query.sector)
        return jobs, scraper.last_outcomes[-1]
    return search


def _ats_adapter(portal: str, settings, log, slice_cache) -> Adapter:
    def search(query: SourceQuery):
        ats = ATSDiscovery(settings, log, slice_cache=slice_cache)
        limit = settings.ats_results_per_probe
        # The dataset has no hours_old; the freshness filter handles age.
        jobs = ats.search(
            query=query.title.title,
            location=query.geo.code if query.geo.level == STATE else None,
            ats=[portal], limit=limit, client_industry=query.sector)
        outcome = SourceOutcome(source=portal, jobs_returned=len(jobs))
        if ats.errors:
            outcome.stop_reason = ERROR
            outcome.error_message = "; ".join(ats.errors)
            return jobs, outcome
        attach_ats_company_sites(jobs, ats, settings, log)
        outcome.stop_reason = BUDGET_REACHED if len(jobs) >= limit else SOURCE_EXHAUSTED
        return jobs, outcome
    return search


def attach_ats_company_sites(jobs: list[RawJob], ats: ATSDiscovery, settings, log) -> None:
    """Fill company websites for ATS rows from the packaged company directory.

    The ATS jobs dataset has no website column, so without this every
    ATS-sourced company is domain-less. Only URLs that normalize to a real
    employer domain are attached - a greenhouse.io careers link identifies the
    platform, so it is left null rather than guessed at.
    """
    if not settings.ats_resolve_company_sites:
        return
    names = sorted({j.company_name for j in jobs if j.company_name and not j.company_website})
    if not names:
        return
    # The directory publishes no location for every row, but the posting
    # does - pass it so a same-named firm elsewhere can be ruled out.
    locations: dict[str, str] = {}
    for job in jobs:
        if job.company_name and job.location:
            locations.setdefault(job.company_name, job.location)
    try:
        sites = ats.resolve_company_sites(names, locations=locations)
    except Exception as exc:
        log.warning("ats_company_sites_error", error=str(exc))
        return
    from nexbase.pipeline.normalize import normalize_domain

    attached = ambiguous = renamed = 0
    for job in jobs:
        match = sites.get(job.company_name or "")
        if not match:
            continue
        # Why this URL was believed - or why it was not - travels with the
        # job, so a wrong attachment is traceable rather than silent.
        job.raw = dict(job.raw or {})
        job.raw["ats_company_match"] = {
            "source": "ATS_DIRECTORY", "status": match["status"],
            "matched_name": match.get("matched_name"),
            "name_similarity": match.get("similarity"),
            "location_match": match.get("location_match"),
            "confidence": match.get("confidence"),
            "reason": match.get("reason"),
            "url": match.get("url"),
        }
        if match["status"] != "MATCHED":
            if match["status"] == "AMBIGUOUS":
                ambiguous += 1
            continue
        # The jobs dataset stores the ATS tenant slug, so the employer reaches
        # the pipeline as "componentrepairtechnologies". The directory
        # publishes the real name; adopt it so the dedup key, the domain search
        # and the lead a human reads all use it.
        matched_name = str(match.get("matched_name") or "").strip()
        if matched_name and matched_name.lower() != (job.company_name or "").lower():
            job.raw["ats_company_slug"] = job.company_name
            job.company_name = matched_name
            renamed += 1
        if not normalize_domain(match["url"] or ""):
            continue
        job.company_website = match["url"]
        attached += 1
    log.info("ats_company_sites_attached", requested=len(names),
             attached=attached, ambiguous=ambiguous, renamed=renamed)
