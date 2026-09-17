"""Direct board adapters JobSpy does not cover: SimplyHired, Talent.com, PostJobFree.

Structured data first (embedded JSON, schema.org ``JobPosting``), presentation
markup only as a fallback, so the adapters survive restyles. All fetching goes
through the Access Layer, so robots, SSRF, rate limits and the Camoufox budget
apply here too.

Verified live on 2026-09-16: a date-posted URL filter is honoured by SimplyHired
(``t``) and Talent.com (``date``), so the freshness window is pushed to the
source. A first page with no job rows is only "no results" when the board says
so; otherwise it is reported as a parse failure.

Nothing is invented: a posting with no parseable date is emitted with
``posted_at=None`` and the freshness filter rejects it, exactly as for any
other source.
"""
from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from nexbase.access.fetcher import AccessLayer
from nexbase.config import get_settings
from nexbase.core.models import RawJob
from nexbase.core.source_tracking import JOB_BOARD, SourceInfo
from nexbase.core.timeutils import coerce_datetime
from nexbase.discovery.coverage import (
    BUDGET_REACHED,
    ERROR,
    SOURCE_EXHAUSTED,
    SourceOutcome,
    classify_fetch_failure,
)
from nexbase.logging_setup import get_logger

# Boards emit both "3 days ago" and the compact "4d" / "2w" / "30m+".
_RELATIVE_AGE = re.compile(
    r"(\d+)\s*\+?\s*(months?|mo|minutes?|mins?|weeks?|hours?|hrs?|days?|m|w|h|d)"
    r"(?![a-z])(?:\s*ago)?",
    re.IGNORECASE,
)
_UNIT = {"m": "minute", "min": "minute", "mins": "minute", "minute": "minute",
         "minutes": "minute", "h": "hour", "hr": "hour", "hrs": "hour",
         "hour": "hour", "hours": "hour", "d": "day", "day": "day",
         "days": "day", "w": "week", "week": "week", "weeks": "week",
         "mo": "month", "month": "month", "months": "month"}
_MONTH_DAY = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b",
    re.IGNORECASE,
)
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
           "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def parse_month_day(text: str | None, now: datetime | None = None) -> datetime | None:
    """Parse a year-less "Sep 12" stamp, as PostJobFree emits.

    The year is resolved deterministically, not guessed: boards omit it only
    for recent postings, so the current year is used unless that lands more
    than a day in the future, in which case the stamp belongs to last year.
    Returns None when nothing parses.
    """
    if not text:
        return None
    match = _MONTH_DAY.search(text)
    if not match:
        return None
    now = now or datetime.now(timezone.utc)
    month = _MONTHS.get(match.group(1).lower()[:3])
    if month is None:
        return None
    day = int(match.group(2))
    for year in (now.year, now.year - 1):
        try:
            candidate = datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            return None
        if (candidate - now).days <= 1:
            return candidate
    return None


_JUST_POSTED = re.compile(r"just posted|today|active today", re.IGNORECASE)


def parse_relative_age(text: str | None, now: datetime | None = None) -> datetime | None:
    """Convert 'Posted 3 days ago' into a timestamp. Returns None if unparseable."""
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    if _JUST_POSTED.search(text):
        return now
    match = _RELATIVE_AGE.search(text)
    if not match:
        return None
    amount = int(match.group(1))
    unit = _UNIT.get(match.group(2).lower())
    if unit is None:
        return None
    delta = {
        "minute": timedelta(minutes=amount),
        "hour": timedelta(hours=amount),
        "day": timedelta(days=amount),
        "week": timedelta(weeks=amount),
        "month": timedelta(days=30 * amount),
    }[unit]
    return now - delta


def _iter_jsonld(soup: BeautifulSoup):
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = [payload]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if "@graph" in node:
                    stack.extend(node["@graph"] if isinstance(node["@graph"], list) else [])
                yield node


def _is_job_posting(node: dict) -> bool:
    atype = node.get("@type")
    types = atype if isinstance(atype, list) else [atype]
    return any(str(t).lower() == "jobposting" for t in types if t)


def _org_name(node: dict) -> str | None:
    org = node.get("hiringOrganization")
    if isinstance(org, dict):
        return org.get("name")
    if isinstance(org, str):
        return org
    return None


def _org_site(node: dict) -> str | None:
    org = node.get("hiringOrganization")
    if isinstance(org, dict):
        url = org.get("sameAs") or org.get("url")
        if isinstance(url, str) and url.startswith("http"):
            return url
    return None


def _location_text(node: dict) -> str | None:
    loc = node.get("jobLocation")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if isinstance(loc, dict):
        addr = loc.get("address")
        if isinstance(addr, dict):
            parts = [
                addr.get("addressLocality"),
                addr.get("addressRegion"),
                addr.get("addressCountry")
                if isinstance(addr.get("addressCountry"), str)
                else None,
            ]
            joined = ", ".join(p for p in parts if p)
            return joined or None
        if isinstance(addr, str):
            return addr
    if isinstance(loc, str):
        return loc
    return None


@dataclass
class BoardConfig:
    site: str
    base_url: str
    search_template: str
    #: Appended when the board honours a date-posted filter, e.g. "&t={days}".
    date_template: str = ""
    #: Text the board prints when a search genuinely has no results (lower case).
    no_results_markers: tuple[str, ...] = ()


#: Date-posted windows verified live on the boards that filter by date.
DATE_FILTER_DAYS = (7, 14)


def date_filter_days(hours_old: int | None) -> int | None:
    """The smallest verified filter covering ``hours_old``; None when none does."""
    if not hours_old:
        return None
    days = -(-hours_old // 24)
    return next((d for d in DATE_FILTER_DAYS if d >= days), None)


class BoardScraper(ABC):
    """Common JSON-LD-first scraping flow for a public job board."""

    source: SourceInfo = JOB_BOARD
    config: BoardConfig

    #: Detail pages fetched per search page. Results cards carry a title and a
    #: company; the description - and therefore any published email address -
    #: only exists on the posting itself. Bounded because this is one extra
    #: fetch per job.
    detail_page_budget: int = 10

    def __init__(self, access: AccessLayer | None = None, logger=None,
                 fetch_details: bool | None = None) -> None:
        self.access = access or AccessLayer()
        self.log = logger or get_logger(f"nexbase.discovery.{self.config.site}")
        from nexbase.config import get_settings

        settings = get_settings()
        self.fetch_details = (
            settings.board_fetch_detail_pages if fetch_details is None else fetch_details
        )
        self.detail_page_budget = settings.board_detail_page_budget
        self.details_fetched = 0

    def _enrich_from_detail_pages(self, jobs: list[RawJob]) -> None:
        """Fill missing descriptions from each posting's own page.

        Only jobs that arrived without a description are fetched, so a board
        that already returns full text costs nothing extra.
        """
        if not self.fetch_details:
            return
        from nexbase.core.models import extract_emails_from_text

        for job in jobs:
            if self.details_fetched >= self.detail_page_budget:
                return
            # Fetch when anything the pipeline needs is missing. SimplyHired
            # cards carry neither: no description and no posting date (so
            # freshness drops the job).
            if not job.application_url:
                continue
            if job.description and job.posted_at:
                continue
            self.details_fetched += 1
            try:
                page = self.access.fetch(job.application_url)
            except Exception as exc:
                self.log.debug("board_detail_error", site=self.config.site,
                               url=job.application_url, error=str(exc))
                continue
            if not page.ok:
                continue

            posting = self._detail_posting(page.html)
            if job.posted_at is None and posting.get("datePosted"):
                job.posted_at = coerce_datetime(posting["datePosted"])
            if not job.employment_type and posting.get("employmentType"):
                value = posting["employmentType"]
                job.employment_type = (
                    ", ".join(str(v) for v in value) if isinstance(value, list)
                    else str(value)
                )

            text = self._detail_text(page.html, posting)
            if text and not job.description:
                job.description = text
            if text:
                job.observed_emails = list(
                    dict.fromkeys(
                        list(job.observed_emails or [])
                        + extract_emails_from_text(text)
                    )
                )
        if self.details_fetched:
            self.log.info("board_details_fetched", site=self.config.site,
                          count=self.details_fetched)

    @staticmethod
    def _detail_posting(html: str) -> dict:
        """The JobPosting JSON-LD node from a posting page, or {}.

        Boards that publish nothing useful in their results cards still emit a
        complete schema.org JobPosting on the posting itself.
        """
        import json as _json

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = _json.loads(script.string or script.get_text() or "")
            except (ValueError, TypeError):
                continue
            for node in (data if isinstance(data, list) else [data]):
                if isinstance(node, dict) and node.get("@type") == "JobPosting":
                    return node
        return {}

    @staticmethod
    def _detail_text(html: str, posting: dict | None = None) -> str | None:
        """Description text from a posting page, JSON-LD first."""
        from bs4 import BeautifulSoup

        if posting and posting.get("description"):
            return BeautifulSoup(
                str(posting["description"]), "html.parser"
            ).get_text(" ", strip=True)[:20000]
        soup = BeautifulSoup(html, "html.parser")
        body = soup.select_one(
            "[class*='description'], [id*='description'], [data-testid*='description'], main"
        )
        return body.get_text(" ", strip=True)[:20000] if body else None

    @classmethod
    def date_window_hours(cls, hours_old: int | None) -> int | None:
        """The date filter this board's search URL enforces, in hours."""
        days = date_filter_days(hours_old)
        return days * 24 if cls.config.date_template and days else None

    def search_url(self, term: str, location: str, page: int = 1,
                   hours_old: int | None = None) -> str:
        url = self.config.search_template.format(
            term=quote_plus(term), location=quote_plus(location), page=page
        )
        days = date_filter_days(hours_old)
        if self.config.date_template and days:
            url += self.config.date_template.format(days=days)
        return url

    def search(
        self,
        search_terms: list[str],
        locations: list[str],
        pages: int | None = None,
        max_results: int | None = None,
        client_industry: str | None = None,
        hours_old: int | None = None,
    ) -> list[RawJob]:
        """Walk every result page the board will serve for each query.

        ``pages`` and ``max_results`` are *our* budgets, not the board's. When
        one of them ends the walk the outcome says BUDGET_REACHED, so a board
        that still had more is never mistaken for one that ran out.
        """
        settings = get_settings()
        page_budget = pages if pages is not None else settings.discovery_max_pages_per_query
        result_budget = (max_results if max_results is not None
                         else settings.discovery_max_results_per_query)

        records: list[RawJob] = []
        seen: set[str] = set()
        self.last_outcomes: list[SourceOutcome] = []

        for term in search_terms:
            for location in locations:
                started = time.monotonic()
                outcome = SourceOutcome(
                    source=f"board:{self.config.site}", query=term,
                    location_scope=location, stop_reason=SOURCE_EXHAUSTED,
                )
                self.last_outcomes.append(outcome)
                accepted_here = 0

                for page in range(1, page_budget + 1):
                    if accepted_here >= result_budget:
                        outcome.stop_reason = BUDGET_REACHED
                        break
                    url = self.search_url(term, location, page, hours_old)
                    self.log.info(
                        "board_search_start",
                        site=self.config.site,
                        term=term,
                        location=location,
                        page=page,
                    )
                    outcome.pages = page
                    try:
                        fetched = self.access.fetch(url)
                    except Exception as exc:
                        outcome.stop_reason = ERROR
                        outcome.error_type = type(exc).__name__
                        outcome.error_message = str(exc)
                        self.log.warning(
                            "board_fetch_error", site=self.config.site, url=url, error=str(exc)
                        )
                        break

                    if not fetched.ok:
                        outcome.stop_reason = classify_fetch_failure(
                            fetched.error, fetched.status)
                        outcome.error_message = fetched.error
                        self.log.warning(
                            "board_fetch_blocked",
                            site=self.config.site,
                            url=url,
                            engine=fetched.engine,
                            error=fetched.error,
                            stop_reason=outcome.stop_reason,
                        )
                        break

                    found = self.parse(fetched.html, url, client_industry=client_industry)
                    if not found and page == 1 and not self._says_no_results(fetched.html):
                        # A layout change looks exactly like an empty search
                        # unless the board's own "no results" text is checked.
                        outcome.stop_reason = ERROR
                        outcome.error_type = "PARSE_FAILED"
                        outcome.error_message = (
                            "no job rows parsed and no 'no results' message on page 1")
                        self.log.warning("board_parse_failed", site=self.config.site, url=url)
                        break
                    self._enrich_from_detail_pages(found)
                    new = 0
                    for job in found:
                        key = job.external_id or job.application_url or ""
                        if not key or key in seen:
                            outcome.duplicates += 1
                            continue
                        seen.add(key)
                        records.append(job)
                        new += 1
                    outcome.jobs_returned += len(found)
                    outcome.jobs_accepted += new
                    accepted_here += new
                    self.log.info(
                        "board_search_complete",
                        site=self.config.site,
                        term=term,
                        location=location,
                        page=page,
                        parsed=len(found),
                        new=new,
                        engine=fetched.engine,
                    )
                    # Nothing parsed, or nothing on this page we had not already
                    # seen: boards repeat the final page rather than 404, so
                    # both mean the result set is finished.
                    if not found or not new:
                        outcome.stop_reason = SOURCE_EXHAUSTED
                        break
                else:
                    # Every allowed page returned new rows - the board had more.
                    outcome.stop_reason = BUDGET_REACHED

                outcome.runtime_seconds = round(time.monotonic() - started, 2)

        self.log.info("board_total", site=self.config.site, count=len(records))
        return records

    def _says_no_results(self, html: str | None) -> bool:
        text = (html or "").lower()
        return any(marker in text for marker in self.config.no_results_markers)

    # ------------------------------------------------------------------
    def parse(
        self, html: str, page_url: str, client_industry: str | None = None
    ) -> list[RawJob]:
        soup = BeautifulSoup(html, "html.parser")
        jobs = [
            self._from_jsonld(node, page_url, client_industry)
            for node in _iter_jsonld(soup)
            if _is_job_posting(node)
        ]
        jobs = [j for j in jobs if j is not None]
        if jobs:
            return jobs
        return self.parse_fallback(soup, page_url, client_industry)

    def _from_jsonld(
        self, node: dict, page_url: str, client_industry: str | None
    ) -> RawJob | None:
        title = node.get("title")
        company = _org_name(node)
        if not title or not company:
            return None

        url = node.get("url") or page_url
        if isinstance(url, str) and url.startswith("/"):
            url = urljoin(self.config.base_url, url)

        posted_at = coerce_datetime(node.get("datePosted"))
        identifier = node.get("identifier")
        if isinstance(identifier, dict):
            identifier = identifier.get("value")

        description = node.get("description")
        if isinstance(description, str):
            description = BeautifulSoup(description, "html.parser").get_text(" ", strip=True)

        return RawJob(
            source_type=self.source.source_type.value,
            source_priority=int(self.source.source_priority.value),
            source_site=self.config.site,
            external_id=str(identifier) if identifier else (url if isinstance(url, str) else None),
            title=str(title).strip(),
            company_name=str(company).strip(),
            company_url=None,
            company_website=_org_site(node),
            location=_location_text(node),
            description=description,
            posted_at=posted_at,
            application_url=url if isinstance(url, str) else page_url,
            apply_url=url if isinstance(url, str) else page_url,
            country=None,
            is_remote=bool(node.get("jobLocationType")),
            company_industry=node.get("industry"),
            search_industry=client_industry,
            company_employee_count=None,
            raw={"jsonld": {k: v for k, v in node.items() if k != "description"}},
        )

    @abstractmethod
    def parse_fallback(
        self, soup: BeautifulSoup, page_url: str, client_industry: str | None
    ) -> list[RawJob]:
        """Presentation-markup fallback when a page carries no JSON-LD."""


class SimplyHiredDiscovery(BoardScraper):
    """SimplyHired.com search results.

    The results page embeds every job in ``__NEXT_DATA__`` with an exact
    ``dateOnIndeed`` timestamp and its job types. Only some cards show a visible
    "6d" stamp, so reading cards alone left 11 of 20 live jobs undated.
    """

    config = BoardConfig(
        site="simplyhired",
        base_url="https://www.simplyhired.com",
        search_template=(
            "https://www.simplyhired.com/search?q={term}&l={location}&pn={page}"
        ),
        date_template="&t={days}",
        no_results_markers=("did not find any",),
    )

    def parse(self, html, page_url, client_industry=None):
        jobs = self._from_next_data(html, client_industry)
        if jobs is not None:
            return jobs
        return super().parse(html, page_url, client_industry)

    def _from_next_data(self, html, client_industry) -> list[RawJob] | None:
        soup = BeautifulSoup(html or "", "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")
        try:
            listed = json.loads(script.string)["props"]["pageProps"]["jobs"]
        except (AttributeError, KeyError, TypeError, ValueError):
            return None
        jobs: list[RawJob] = []
        for item in listed or []:
            if not isinstance(item, dict) or not item.get("title"):
                continue
            url = urljoin(self.config.base_url, item.get("botUrl") or "") if item.get("botUrl") else None
            posted = None
            if item.get("dateOnIndeed"):
                try:
                    posted = datetime.fromtimestamp(int(item["dateOnIndeed"]) / 1000, timezone.utc)
                except (TypeError, ValueError, OverflowError):
                    posted = None
            job_types = item.get("jobTypes") or []
            jobs.append(RawJob(
                source_type=self.source.source_type.value,
                source_priority=int(self.source.source_priority.value),
                source_site=self.config.site,
                external_id=url or item.get("jobKey"),
                title=item.get("title"),
                company_name=item.get("company") or None,
                location=item.get("location") or None,
                posted_at=posted,
                employment_type=", ".join(map(str, job_types)) if isinstance(job_types, list) and job_types else None,
                application_url=url,
                apply_url=url,
                search_industry=client_industry,
                raw={"extraction": "simplyhired-next-data", "job_key": item.get("jobKey"),
                     "snippet": item.get("snippet"), "salary": item.get("salaryInfo"),
                     "date_source": "dateOnIndeed"},
            ))
        return jobs

    def parse_fallback(self, soup, page_url, client_industry):
        jobs: list[RawJob] = []
        for card in soup.select("[data-testid='searchSerpJob'], li.css-0 div.SerpJob-jobCard"):
            link = card.find("a", href=True)
            title_el = card.select_one("[data-testid='searchSerpJobTitle'], h3 a, .jobposting-title")
            company_el = card.select_one("[data-testid='companyName'], .jobposting-company")
            location_el = card.select_one("[data-testid='searchSerpJobLocation'], .jobposting-location")
            age_el = card.select_one("[data-testid='searchSerpJobDateStamp'], [data-testid='detailText']")
            if not (title_el and company_el):
                continue
            url = urljoin(self.config.base_url, link["href"]) if link else page_url
            jobs.append(
                RawJob(
                    source_type=self.source.source_type.value,
                    source_priority=int(self.source.source_priority.value),
                    source_site=self.config.site,
                    external_id=url,
                    title=title_el.get_text(strip=True),
                    company_name=company_el.get_text(strip=True),
                    location=location_el.get_text(strip=True) if location_el else None,
                    posted_at=parse_relative_age(age_el.get_text(strip=True) if age_el else None),
                    application_url=url,
                    apply_url=url,
                    search_industry=client_industry,
                    raw={"extraction": "simplyhired-card"},
                )
            )
        return jobs


class TalentComDiscovery(BoardScraper):
    """Talent.com search results.

    Added 2026-09-14 after a live coverage probe. Chosen over CareerBuilder,
    Nexxt, Craigslist, USAJOBS, Snagajob, Jobcase and iHire because it is the
    only candidate that server-renders every field the pipeline needs: title,
    company, location and an exact ISO timestamp in a ``<time datetime>``
    attribute (20/20 dated on the probe). robots.txt permits the search path
    and Scrapling fetches it without the Camoufox fallback.

    Aggregator caveat: listings carry no employer website, so domain
    resolution falls back to the apply URL - the same limitation LinkedIn has.
    """

    config = BoardConfig(
        site="talent_com",
        base_url="https://www.talent.com",
        # radius is in km and accepts only 10 / 25 / 50 / 100 (5-60 mi); without it
        # a state is one point with a 15 mi radius. Live 2026-09-17, "forklift
        # operator" in Ohio: 1 result without it, 20 across Ohio with 100.
        search_template=(
            "https://www.talent.com/jobs?k={term}&l={location}&radius=100&p={page}"
        ),
        date_template="&date={days}",
        no_results_markers=("no results for",),
    )

    def parse_fallback(self, soup, page_url, client_industry):
        jobs: list[RawJob] = []
        for card in soup.select("[data-testid='job-card-unified']"):
            title_el = card.select_one("[class*='JobCard_title']")
            company_el = card.select_one("[class*='JobCard_company']")
            location_el = card.select_one("[class*='JobCard_location']")
            if not (title_el and company_el):
                continue

            link = card.find("a", href=True)
            url = urljoin(self.config.base_url, link["href"]) if link else page_url

            # Exact timestamp, so no relative-age guessing is needed.
            time_el = card.find("time")
            posted_at = coerce_datetime(time_el.get("datetime")) if time_el else None
            if posted_at is None and time_el is not None:
                posted_at = parse_relative_age(time_el.get_text(strip=True))

            desc_el = card.select_one("[class*='JobCard_snippet'], [class*='JobCard_description']")

            jobs.append(
                RawJob(
                    source_type=self.source.source_type.value,
                    source_priority=int(self.source.source_priority.value),
                    source_site=self.config.site,
                    external_id=url,
                    title=title_el.get_text(strip=True),
                    company_name=company_el.get_text(strip=True),
                    location=location_el.get_text(strip=True) if location_el else None,
                    description=desc_el.get_text(" ", strip=True) if desc_el else None,
                    posted_at=posted_at,
                    application_url=url,
                    apply_url=url,
                    search_industry=client_industry,
                    raw={"extraction": "talent_com-card"},
                )
            )
        return jobs

class PostJobFreeDiscovery(BoardScraper):
    """PostJobFree.com search results.

    Added 2026-09-14. Server-renders every required field in plain markup:
    title, company (``.colorCompany``), location (``.colorLocation``),
    description snippet and a year-less date stamp (``.colorDate``, e.g.
    "Sep 12") resolved by :func:`parse_month_day`. Scrapling fetches it
    without the Camoufox fallback.

    Aggregator caveat: no employer website is published, so domain resolution
    falls back to the apply URL - same limitation as LinkedIn and Talent.com.
    """

    config = BoardConfig(
        site="postjobfree",
        base_url="https://www.postjobfree.com",
        search_template="https://www.postjobfree.com/jobs?q={term}&l={location}&p={page}",
        no_results_markers=("did not match any jobs",),
    )

    def parse_fallback(self, soup, page_url, client_industry):
        jobs: list[RawJob] = []
        for card in soup.select("div.snippetPadding"):
            title_el = card.select_one("h3.itemTitle a, .itemTitle a")
            company_el = card.select_one(".colorCompany")
            if not (title_el and company_el):
                continue

            location_el = card.select_one(".colorLocation")
            date_el = card.select_one(".colorDate")
            desc_el = card.select_one(".jdSnippet")
            href = title_el.get("href") or ""
            url = urljoin(self.config.base_url, href) if href else page_url

            jobs.append(
                RawJob(
                    source_type=self.source.source_type.value,
                    source_priority=int(self.source.source_priority.value),
                    source_site=self.config.site,
                    external_id=url,
                    title=title_el.get_text(strip=True),
                    company_name=company_el.get_text(strip=True),
                    location=location_el.get_text(strip=True) if location_el else None,
                    description=desc_el.get_text(" ", strip=True) if desc_el else None,
                    posted_at=parse_month_day(
                        date_el.get_text(strip=True) if date_el else None
                    ),
                    application_url=url,
                    apply_url=url,
                    search_industry=client_industry,
                    raw={"extraction": "postjobfree-card"},
                )
            )
        return jobs

#: Every board adapter NexBase implements, by portal.
BOARD_SCRAPERS = {
    "simplyhired": SimplyHiredDiscovery,
    "talent_com": TalentComDiscovery,
    "postjobfree": PostJobFreeDiscovery,
}
