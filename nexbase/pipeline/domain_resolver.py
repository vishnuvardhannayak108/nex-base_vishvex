"""Official company-domain resolution.

85% of size-resolution candidates arrive with no domain: the aggregator
adapters (talent.com, SimplyHired, PostJobFree) publish a company name and
nothing else. Without a domain there is no site to read, so domain coverage -
not size extraction - is the upstream bottleneck.

This resolver establishes the employer's *official* domain, or returns nothing.
It never blindly accepts a search result: every candidate is fetched and scored
against the company name, location and on-page branding, and only HIGH/MEDIUM
confidence results are attached to a profile.

Reuses the existing Access Layer (Scrapling first, single-page Camoufox
fallback, shared rate limiter and budget). No paid API, no proxy.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import quote_plus, urlsplit

from nexbase.logging_setup import get_logger
from nexbase.pipeline.normalize import (
    extract_host,
    is_employer_domain,
    normalize_company_name,
    normalize_domain,
    registrable_domain,
)

_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_PHONE = re.compile(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")

#: US state names and abbreviations, for location corroboration.
_STATES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn",
    "mississippi": "ms", "missouri": "mo", "montana": "mt", "nebraska": "ne",
    "nevada": "nv", "new hampshire": "nh", "new jersey": "nj",
    "new mexico": "nm", "new york": "ny", "north carolina": "nc",
    "north dakota": "nd", "ohio": "oh", "oklahoma": "ok", "oregon": "or",
    "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
    "vermont": "vt", "virginia": "va", "washington": "wa",
    "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
}

#: Words dropped before comparing a company name to a domain.
_NAME_NOISE = {
    "inc", "llc", "ltd", "corp", "corporation", "company", "co", "group",
    "holdings", "enterprises", "industries", "services", "solutions",
    "international", "usa", "us", "the", "and", "of",
}


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


#: Only this may be attached to a company profile as a verified domain.
#: MEDIUM used to attach too, which let a zero-name-affinity match through on
#: page branding alone (american scaffolding -> amscaf.com). A wrong domain
#: sends contact discovery to another company's website, so anything short of
#: an unambiguous match is a suggestion for review instead.
ACCEPTABLE = (Confidence.HIGH,)

#: Recorded for a human to confirm, never attached automatically.
REVIEWABLE = (Confidence.MEDIUM,)


@dataclass
class DomainResult:
    domain: str | None
    confidence: Confidence
    source: str
    evidence_url: str | None = None
    evidence_snippet: str | None = None
    reason: str = ""
    signals: dict = field(default_factory=dict)

    @property
    def acceptable(self) -> bool:
        """True only for a match strong enough to attach without a human."""
        if not self.domain or self.confidence not in ACCEPTABLE:
            return False
        # The affinity guard applies only to domains we went looking for. A
        # domain the SOURCE supplied (EXISTING, SOURCE_URL) is observed
        # evidence, not a guess, and carries no affinity score at all.
        if self.source != "SEARCH" or self.signals.get("cached"):
            return True
        # No HIGH branch of `_score` can reach zero affinity, but a domain
        # whose label bears no relation to the company name must never be
        # attached on page branding alone.
        return self.signals.get("name_affinity", 0.0) > 0.0

    @property
    def needs_review(self) -> bool:
        """A plausible candidate that is not certain enough to attach."""
        if not self.domain:
            return False
        if self.confidence in REVIEWABLE:
            return True
        return (
            self.confidence in ACCEPTABLE
            and self.source == "SEARCH"
            and not self.signals.get("cached")
            and not self.signals.get("name_affinity", 0.0)
        )


def _slug(text: str) -> str:
    return _NON_ALNUM.sub("", (text or "").lower())


def _name_tokens(company_name: str) -> list[str]:
    words = normalize_company_name(company_name).split()
    return [w for w in words if w not in _NAME_NOISE and len(w) > 2]


def name_domain_affinity(company_name: str, domain: str) -> float:
    """0..1 — how much of the company name the domain's label accounts for."""
    label = _slug(registrable_domain(domain).split(".")[0])
    if not label:
        return 0.0
    tokens = _name_tokens(company_name)
    if not tokens:
        return 0.0
    joined = _slug("".join(tokens))
    if label == joined:
        return 1.0
    if joined and (joined.startswith(label) or label.startswith(joined)):
        return 0.9
    matched = sum(1 for t in tokens if _slug(t) and _slug(t) in label)
    ratio = matched / len(tokens)
    # Initialism: "acme metal fabrication" -> "amf"
    if ratio == 0 and len(tokens) >= 2:
        if label == "".join(t[0] for t in tokens):
            return 0.7
    return round(ratio, 2)


def name_similarity(left: str | None, right: str | None) -> float:
    """0..1 overlap between two company names, legal noise removed.

    Used to check that a directory match is actually the same employer:
    "Summit Construction" and "Summit Construction Group of Texas" share every
    token of the shorter name, while "Summit Construction" and "Summit Dental"
    do not.
    """
    left_tokens, right_tokens = _name_tokens(left or ""), _name_tokens(right or "")
    if not left_tokens or not right_tokens:
        return 0.0
    # An ATS slug has no word breaks, so it shares no token with the name it
    # was made from: "componentrepairtechnologies" scored 0.0 against
    # "Component Repair Technologies" and the directory match was refused.
    if _slug("".join(left_tokens)) == _slug("".join(right_tokens)):
        return 1.0
    a, b = set(left_tokens), set(right_tokens)
    return round(len(a & b) / min(len(a), len(b)), 2)


def location_tokens(location: str | None) -> set[str]:
    if not location:
        return set()
    lowered = location.lower()
    tokens = {p.strip() for p in re.split(r"[,/|]", lowered) if p.strip()}
    for name, abbr in _STATES.items():
        if name in lowered:
            tokens |= {name, abbr}
        elif re.search(rf"\b{abbr}\b", lowered):
            tokens |= {name, abbr}
    return {t for t in tokens if len(t) >= 2}


class DomainResolver:
    """Resolves and validates an employer's official domain."""

    #: Hosts that are never an employer's own site.
    _SEARCH_NOISE = (
        "indeed", "linkedin", "glassdoor", "ziprecruiter", "simplyhired",
        "talent.com", "postjobfree", "monster", "jobrapido", "careerjet",
        "facebook", "instagram", "twitter", "x.com", "youtube", "wikipedia",
        "bloomberg", "crunchbase", "zoominfo", "apollo.io", "dnb.com",
        "manta.com", "yelp", "bbb.org", "mapquest", "buzzfile", "rocketreach",
        "signalhire", "leadiq", "zippia", "comparably", "trustpilot",
        "bing.com", "duckduckgo", "brave.com", "google.com", "yahoo.com",
        "mojeek", "startpage", "ecosia", "microsoft.com", "msn.com",
    )

    def __init__(self, access=None, settings=None, logger=None) -> None:
        from nexbase.access.fetcher import AccessLayer
        from nexbase.config import get_settings

        self.settings = settings or get_settings()
        self.access = access or AccessLayer(settings=self.settings)
        self.log = logger or get_logger("nexbase.pipeline.domain_resolver")
        self.searches = 0
        self.validations = 0

    # ------------------------------------------------------------------
    def resolve(
        self,
        company_name: str,
        existing_domain: str | None = None,
        source_urls: list[str] | None = None,
        location: str | None = None,
    ) -> DomainResult:
        """Resolve the official domain, strongest evidence first."""
        # 1. Already known.
        if existing_domain and is_employer_domain(existing_domain):
            return DomainResult(existing_domain, Confidence.HIGH, "EXISTING",
                                reason="domain already established")

        # 2/3. A source URL that identifies the employer rather than a platform.
        for url in source_urls or []:
            candidate = normalize_domain(url)
            if candidate:
                return DomainResult(candidate, Confidence.HIGH, "SOURCE_URL",
                                    evidence_url=url,
                                    reason="employer URL supplied by the source")

        # 4. Public-web search, then validate.
        candidates = self._search(company_name, location)
        if not candidates:
            return DomainResult(None, Confidence.LOW, "SEARCH",
                                reason="no candidate domains found")

        best: DomainResult | None = None
        for candidate in candidates[: self.settings.domain_max_candidates]:
            result = self.validate(candidate, company_name, location)
            if best is None or _rank(result) > _rank(best):
                best = result
            if result.confidence is Confidence.HIGH:
                break

        assert best is not None
        if not best.acceptable:
            self.log.info("domain_rejected", company=company_name,
                          candidate=best.domain, reason=best.reason)
            return DomainResult(None, Confidence.LOW, "SEARCH",
                                evidence_url=best.evidence_url,
                                reason=f"best candidate rejected: {best.reason}")
        self.log.info("domain_resolved", company=company_name,
                      domain=best.domain, confidence=best.confidence.value)
        return best

    # ------------------------------------------------------------------
    #: Public search endpoints, tried in order until one answers. No API key,
    #: no proxy. DuckDuckGo is last: its HTML endpoint serves a CAPTCHA
    #: interstitial (HTTP 202) to us as of 2026-09-14.
    SEARCH_ENGINES = (
        # Brave publishes result URLs directly; Bing wraps every result in a
        # base64 /ck/a redirect, and DuckDuckGo's HTML endpoint CAPTCHA-gates us.
        ("brave", "https://search.brave.com/search?q={q}"),
        ("bing", "https://www.bing.com/search?q={q}"),
        ("duckduckgo", "https://html.duckduckgo.com/html/?q={q}"),
    )

    def _search(self, company_name: str, location: str | None) -> list[str]:
        """Candidate domains from public search, noise removed.

        Engines are tried in order; the first that returns usable results wins.
        Results are candidates only - every one is validated before use.
        """
        query = f"{company_name} {location}" if location else company_name
        query = f"{query} official site"
        encoded = quote_plus(query)

        for name, template in self.SEARCH_ENGINES:
            try:
                page = self.access.fetch(template.format(q=encoded))
            except Exception as exc:
                self.log.debug("domain_search_error", engine=name, error=str(exc))
                continue
            self.searches += 1
            if not page.ok:
                self.log.debug("domain_search_blocked", engine=name, status=page.status)
                continue
            candidates = self._extract_candidates(page.html)
            if candidates:
                self.log.debug("domain_search_ok", engine=name, found=len(candidates))
                return candidates
        return []

    def _extract_candidates(self, html: str) -> list[str]:
        from urllib.parse import unquote

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        seen: dict[str, None] = {}
        for anchor in soup.select("a[href]"):
            href = anchor["href"]
            href = _unwrap_redirect(href)
            if not href.startswith("http"):
                continue
            host = extract_host(href)
            if not host or any(n in host for n in self._SEARCH_NOISE):
                continue
            domain = normalize_domain(href)
            if domain:
                seen.setdefault(domain, None)
        return list(seen)

    # ------------------------------------------------------------------
    def validate(
        self, domain: str, company_name: str, location: str | None = None
    ) -> DomainResult:
        """Fetch the candidate and score it against name, location and branding."""
        affinity = name_domain_affinity(company_name, domain)
        signals: dict = {"name_affinity": affinity}

        try:
            page = self.access.fetch(f"https://{domain}")
        except Exception as exc:
            return DomainResult(domain, Confidence.LOW, "SEARCH",
                                reason=f"unreachable: {type(exc).__name__}",
                                signals=signals)
        self.validations += 1
        if not page.ok:
            return DomainResult(domain, Confidence.LOW, "SEARCH",
                                reason=f"blocked or empty (status={page.status})",
                                signals=signals)

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(page.html, "html.parser")
        text = _WS.sub(" ", soup.get_text(" ", strip=True)).lower()
        title = (soup.title.get_text(strip=True) if soup.title else "").lower()

        tokens = _name_tokens(company_name)
        branding = bool(tokens) and all(t in text for t in tokens)
        branding_title = bool(tokens) and any(t in title for t in tokens)
        loc_tokens = location_tokens(location)
        location_hit = bool(loc_tokens & {t for t in loc_tokens if t in text})
        has_phone = bool(_PHONE.search(text))

        signals.update({
            "branding_in_body": branding, "branding_in_title": branding_title,
            "location_match": location_hit, "has_phone": has_phone,
            "title": title[:80],
        })

        confidence, reason = self._score(
            affinity, branding, branding_title, location_hit
        )
        snippet = title or text[:120]
        return DomainResult(domain, confidence, "SEARCH", evidence_url=page.url,
                            evidence_snippet=snippet, reason=reason, signals=signals)

    @staticmethod
    def _score(affinity, branding, branding_title, location_hit):
        """Name affinity is necessary; branding and location corroborate it."""
        if affinity >= 0.9 and (branding or branding_title):
            if location_hit or branding:
                return Confidence.HIGH, "name matches domain and page branding"
            return Confidence.MEDIUM, "name matches domain, weak corroboration"
        if affinity >= 0.5 and branding and location_hit:
            return Confidence.HIGH, "partial name match with branding and location"
        if affinity >= 0.5 and (branding or branding_title):
            return Confidence.MEDIUM, "partial name match with branding"
        if affinity >= 0.9:
            return Confidence.MEDIUM, "name matches domain, no branding found"
        if branding and location_hit:
            return Confidence.MEDIUM, "branding and location match, weak domain match"
        return Confidence.LOW, (
            f"insufficient evidence (affinity={affinity}, branding={branding}, "
            f"location={location_hit})"
        )


def _unwrap_redirect(href: str) -> str:
    """Unwrap an engine's result redirect to the real destination URL.

    DuckDuckGo uses ``/l/?uddg=<percent-encoded>``; Bing uses
    ``/ck/a?...&u=a1<base64>``. Left wrapped, every Bing result looks like
    bing.com and is discarded as noise.
    """
    from urllib.parse import unquote

    match = re.search(r"[?&]uddg=([^&]+)", href)
    if match:
        return unquote(match.group(1))

    match = re.search(r"[?&]u=a1([A-Za-z0-9_\-]+)", href)
    if match:
        import base64

        raw = match.group(1).replace("-", "+").replace("_", "/")
        try:
            return base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "ignore")
        except Exception:
            return href
    return href


def _rank(result: DomainResult) -> tuple[int, float]:
    order = {Confidence.HIGH: 2, Confidence.MEDIUM: 1, Confidence.LOW: 0}
    return order[result.confidence], result.signals.get("name_affinity", 0.0)


class NullDomainResolver:
    """Default. Resolves nothing, costs nothing."""

    def resolve(self, company_name, existing_domain=None, source_urls=None,
                location=None) -> DomainResult:
        if existing_domain and is_employer_domain(existing_domain):
            return DomainResult(existing_domain, Confidence.HIGH, "EXISTING")
        return DomainResult(None, Confidence.LOW, "DISABLED", reason="resolver disabled")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


@dataclass
class DomainCache:
    """Reuses a previously verified official domain instead of re-searching.

    Reuses ``companies.updated_at``-style freshness via ``domain_resolved_at``.
    A cached entry is valid only when it is a plausible employer domain, was
    accepted at HIGH confidence, and is not stale. MEDIUM is deliberately not
    reusable: it never became the company's domain in the first place.
    """

    repo: object = None
    max_age_days: int = 180
    #: How long a LOW/unresolved attempt suppresses another search.
    negative_max_age_days: int = 7
    hits: int = 0
    negative_hits: int = 0

    def lookup(self, normalized_domain: str, normalized_name: str,
               now=None) -> DomainResult | None:
        if self.repo is None or not getattr(self.repo, "configured", False):
            return None
        row = self.repo.find_company(normalized_domain, normalized_name)
        if not row:
            return None
        return self.from_row(row, now=now)

    def suppresses_search(self, normalized_domain: str, normalized_name: str,
                          now=None) -> bool:
        """True when a recent failed attempt should stop us searching again.

        Only a *failed* attempt counts. A row carrying a verified domain is
        served by :meth:`lookup`, and a row that was never attempted is not
        suppressed at all - absence of a domain is not evidence of failure.
        """
        if self.repo is None or not getattr(self.repo, "configured", False):
            return False
        row = self.repo.find_company(normalized_domain, normalized_name) or {}
        return self.is_negative_cached(row, now=now)

    def is_negative_cached(self, row: dict, now=None) -> bool:
        row = row or {}
        if row.get("domain_confidence") != Confidence.LOW.value:
            return False
        if not self._within(row.get("domain_resolved_at"),
                            self.negative_max_age_days, now):
            return False
        self.negative_hits += 1
        return True

    def from_row(self, row: dict, now=None) -> DomainResult | None:
        domain = (row or {}).get("domain")
        confidence = (row or {}).get("domain_confidence")
        if not domain or not is_employer_domain(domain):
            return None
        if confidence not in {c.value for c in ACCEPTABLE}:
            return None
        if not self._fresh(row.get("domain_resolved_at"), now):
            return None
        self.hits += 1
        return DomainResult(
            domain=domain,
            confidence=Confidence(confidence),
            source=row.get("domain_source") or "CACHE",
            evidence_url=row.get("domain_evidence_url"),
            reason="reused verified domain from a previous run",
            # A persisted row was only written because it passed the policy at
            # resolution time, so it is not re-judged on an affinity score the
            # row never stored. Flagged rather than given an invented number.
            signals={"cached": True},
        )

    def _fresh(self, stamp, now) -> bool:
        return self._within(stamp, self.max_age_days, now)

    @staticmethod
    def _within(stamp, max_age_days: int, now) -> bool:
        from datetime import datetime, timedelta, timezone

        if not stamp:
            return False
        now = now or datetime.now(timezone.utc)
        try:
            seen = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            return False
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        return (now - seen) <= timedelta(days=max_age_days)


def negative_domain_row(now=None) -> dict:
    """Columns recording a failed attempt so it is not repeated immediately.

    No domain is written - only the LOW verdict and its timestamp - so a failed
    attempt can never be mistaken for a verified domain.
    """
    from datetime import datetime, timezone

    return {
        "domain_confidence": Confidence.LOW.value,
        "domain_source": "SEARCH",
        "domain_resolved_at": (now or datetime.now(timezone.utc)).isoformat(),
    }


def domain_row(result: DomainResult, now=None) -> dict:
    """Columns to persist for a verified domain. LOW is never written."""
    from datetime import datetime, timezone

    if not result.acceptable:
        return {}
    return {
        "domain": result.domain,
        "domain_confidence": result.confidence.value,
        "domain_source": result.source,
        "domain_evidence_url": result.evidence_url,
        "domain_resolved_at": (now or datetime.now(timezone.utc)).isoformat(),
    }
