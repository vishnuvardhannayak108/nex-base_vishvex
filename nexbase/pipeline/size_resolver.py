"""Employee-size resolution from free, public evidence.

64% of fresh companies arrive with no headcount: only Indeed publishes
``company_num_employees``, and the aggregator adapters publish none. Those
companies are not failing the configured size rule - they were never measurable.

This module resolves size from the employer's own public pages, reusing the
existing Access Layer (Scrapling first, single-page Camoufox fallback, shared
rate limiter and budget). No new provider, no paid API, no proxy.

**Only explicit statements count.** A number is accepted when the surrounding
text names employees/staff/workforce. Revenue, square footage, number of
locations, follower counts, years in business and hiring volume are never read
as headcount - each is a well-known way to be confidently wrong.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from nexbase.logging_setup import get_logger

# ---------------------------------------------------------------------------
# Explicit-evidence patterns
# ---------------------------------------------------------------------------

_NUM = r"(\d{1,3}(?:,\d{3})*|\d+)"
#: Words that make a nearby number a headcount rather than anything else.
_PEOPLE = r"(?:employees?|staff|team\s+members?|people|associates?|workers?|" \
          r"personnel|workforce|colleagues|professionals?|employees\s+worldwide)"

#: "50-200 employees", "50 to 200 employees"
_RANGE_RE = re.compile(
    rf"{_NUM}\s*(?:-|–|—|to)\s*{_NUM}\s+{_PEOPLE}", re.IGNORECASE)
#: "over 500 employees", "more than 500 staff"
_MORE_RE = re.compile(
    rf"(?:over|more\s+than|greater\s+than|upwards\s+of|\+)\s*{_NUM}\s+{_PEOPLE}",
    re.IGNORECASE)
#: "500+ employees"
_PLUS_RE = re.compile(rf"{_NUM}\s*\+\s*{_PEOPLE}", re.IGNORECASE)
#: "fewer than 50 employees"
_FEWER_RE = re.compile(
    rf"(?:fewer\s+than|less\s+than|under|up\s+to)\s*{_NUM}\s+{_PEOPLE}", re.IGNORECASE)
#: "employs 120 people", "team of 120", "120 employees"
_EXACT_RE = re.compile(
    rf"(?:{_NUM}\s+{_PEOPLE}"
    rf"|(?:employs|employing|team\s+of|staff\s+of|workforce\s+of)\s+(?:over\s+|about\s+|approximately\s+|around\s+|nearly\s+)?{_NUM})",
    re.IGNORECASE)

#: Units that can immediately PRECEDE a number and change what it measures.
#: The pattern already requires an explicit people-word next to the number, so
#: "200 locations" cannot match at all; this only guards against a money or
#: measurement qualifier attached to the figure itself. Kept deliberately tight
#: - a wide window rejected real prose like "employs 140 people ... serving
#: industrial customers", where "customers" was merely nearby.
_DISQUALIFYING = (
    "revenue", "sales of", "$", "usd", "eur", "million in", "billion in",
    "sq ft", "square feet", "square foot", "square metres", "acres",
    "per year", "annually", "per hour", "per week",
)

#: Characters inspected immediately before the match for a disqualifying unit.
_PRE_WINDOW = 30
#: Characters inspected immediately after.
_POST_WINDOW = 12

#: Pages most likely to state headcount, highest-yield first.
CANDIDATE_PATHS = (
    "/about", "/about-us", "/company", "/who-we-are", "/our-story",
    "/careers", "/about/company", "/company/about",
)

#: Link text worth following one hop from the homepage.
_LINK_HINTS = ("about", "company", "who we are", "our story", "careers", "team")

#: Sanity bounds. Outside these a "headcount" is almost certainly another metric.
MIN_PLAUSIBLE, MAX_PLAUSIBLE = 1, 3_000_000

_WS = re.compile(r"\s+")


def _to_int(raw: str) -> int | None:
    try:
        return int(raw.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _plausible(value: int | None) -> bool:
    return value is not None and MIN_PLAUSIBLE <= value <= MAX_PLAUSIBLE


def _context(text: str, start: int, end: int) -> str:
    """Tight window around the figure, not the whole sentence."""
    return text[max(0, start - _PRE_WINDOW):end + _POST_WINDOW].lower()


def extract_employee_size(text: str | None) -> tuple[int | None, int | None, str] | None:
    """Return ``(min, max, evidence_snippet)`` from explicit statements only.

    Returns ``None`` when nothing explicit is found. Never infers.
    """
    if not text:
        return None
    cleaned = _WS.sub(" ", text)

    for pattern, kind in (
        (_RANGE_RE, "range"), (_FEWER_RE, "fewer"), (_MORE_RE, "more"),
        (_PLUS_RE, "plus"), (_EXACT_RE, "exact"),
    ):
        for match in pattern.finditer(cleaned):
            context = _context(cleaned, match.start(), match.end())
            if any(bad in context for bad in _DISQUALIFYING):
                continue

            groups = [g for g in match.groups() if g]
            values = [_to_int(g) for g in groups]
            if not values or not all(_plausible(v) for v in values):
                continue

            snippet = cleaned[max(0, match.start() - 40):match.end() + 40].strip()
            if kind == "range" and len(values) >= 2:
                lo, hi = sorted(values[:2])
                return lo, hi, snippet
            if kind == "fewer":
                return None, values[0], snippet
            if kind in ("more", "plus"):
                return values[0], None, snippet
            return values[0], values[0], snippet
    return None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@dataclass
class SizeEvidence:
    employee_size_min: int | None
    employee_size_max: int | None
    source: str
    url: str | None = None
    snippet: str | None = None

    def as_enrichment(self) -> dict:
        """Shape expected by ``build_profile(..., enrichment=...)``."""
        return {
            "employee_size_min": self.employee_size_min,
            "employee_size_max": self.employee_size_max,
            "provider": self.source,
        }


class SizeResolver(Protocol):
    def resolve(self, domain: str | None, company_name: str) -> SizeEvidence | None: ...


class NullSizeResolver:
    """Default. Resolves nothing, costs nothing."""

    def resolve(self, domain: str | None, company_name: str) -> SizeEvidence | None:
        return None


# ---------------------------------------------------------------------------
# Public-web resolver
# ---------------------------------------------------------------------------


class WebSizeResolver:
    """Reads headcount off the employer's own site, subdomains only as fallback.

    Order is fixed: the main company website is always tried first, and
    subdomain discovery runs only when the main site yielded no explicit
    statement. A certificate-transparency entry is a candidate, never evidence.
    """

    source = "PUBLIC_WEB"
    subdomain_source = "PUBLIC_WEB_SUBDOMAIN"

    def __init__(self, access=None, logger=None, max_pages: int | None = None,
                 respect_robots: bool = True, settings=None,
                 scanner=None, allow_subdomains: bool | None = None) -> None:
        from nexbase.config import get_settings
        from nexbase.pipeline.site_evidence import SiteEvidenceScanner

        self.settings = settings or get_settings()
        self.log = logger or get_logger("nexbase.pipeline.size_resolver")
        self.max_pages = max_pages or self.settings.size_resolver_max_pages
        self.allow_subdomains = (
            self.settings.subdomain_fallback_enabled
            if allow_subdomains is None else allow_subdomains
        )
        self.scanner = scanner or SiteEvidenceScanner(
            access=access, settings=self.settings, logger=self.log,
            respect_robots=respect_robots,
        )
        self.pages_fetched = 0
        self.subdomain_fallbacks = 0

    # -- resolution ------------------------------------------------------
    def resolve(self, domain: str | None, company_name: str) -> SizeEvidence | None:
        if not domain:
            return None

        matcher = lambda text: extract_employee_size(text) is not None

        # 1. Main company website - always first.
        main = self.scanner.scan_main_site(domain, self.max_pages, matcher=matcher)
        self.pages_fetched += main.pages_fetched
        evidence = self._first_match(main, self.source)
        if evidence is not None:
            self.log.info("size_resolved", company=company_name, url=evidence.url,
                          via="MAIN_SITE")
            return evidence

        # 2. Subdomains - fallback only, and only because the main site failed.
        if not self.allow_subdomains:
            self.log.info("size_unresolved", company=company_name, domain=domain,
                          subdomain_fallback="disabled")
            return None

        self.subdomain_fallbacks += 1
        subs = self.scanner.scan_subdomains(
            domain, self.settings.subdomain_page_budget, matcher=matcher,
            company_name=company_name,
        )
        self.pages_fetched += subs.pages_fetched
        evidence = self._first_match(subs, self.subdomain_source)
        if evidence is not None:
            self.log.info("size_resolved", company=company_name, url=evidence.url,
                          via="SUBDOMAIN")
            return evidence

        self.log.info("size_unresolved", company=company_name, domain=domain,
                      subdomains_checked=len(subs.subdomains_checked))
        return None

    @staticmethod
    def _first_match(scan, source: str) -> SizeEvidence | None:
        for page in scan.pages:
            found = extract_employee_size(page.text)
            if found is not None:
                lo, hi, snippet = found
                return SizeEvidence(lo, hi, source, url=page.url, snippet=snippet)
        return None


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


@dataclass
class SizeCache:
    """Previously verified sizes, reused inside a freshness window.

    Reuses ``companies.updated_at``; no schema change.
    """

    repo: object = None
    max_age_days: int = 90
    hits: int = 0

    def lookup(self, normalized_domain: str, normalized_name: str,
               now: datetime | None = None) -> SizeEvidence | None:
        if self.repo is None or not getattr(self.repo, "configured", False):
            return None
        row = self.repo.find_company(normalized_domain, normalized_name)
        if not row:
            return None
        lo, hi = row.get("employee_size_min"), row.get("employee_size_max")
        if lo is None and hi is None:
            return None
        if not self._fresh(row.get("updated_at"), now):
            return None
        self.hits += 1
        return SizeEvidence(lo, hi, row.get("employee_size_source") or "CACHE")

    def _fresh(self, stamp, now: datetime | None) -> bool:
        if not stamp:
            return False
        now = now or datetime.now(timezone.utc)
        try:
            seen = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            return False
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        return (now - seen) <= timedelta(days=self.max_age_days)
