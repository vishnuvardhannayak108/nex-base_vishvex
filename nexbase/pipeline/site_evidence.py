"""Targeted company-website evidence discovery.

The main company website is ALWAYS the first evidence source. Subdomain
discovery is a fallback that runs only when the main site fails to yield the
required evidence - it is not part of the happy path.

This is targeted discovery, not a crawler: every stage is page-budgeted, and
pages are chosen by relevance (sitemap, robots, homepage links) rather than by
following everything.

Reuses the existing Access Layer, so Scrapling runs first and Camoufox stays a
single-page fallback.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from nexbase.logging_setup import get_logger

#: Paths worth trying on any company site, highest-yield first.
PRIORITY_PATHS = (
    "/about", "/about-us", "/company", "/who-we-are", "/our-story",
    "/careers", "/jobs", "/employment", "/contact", "/contact-us",
)

#: Link text worth following one hop from the homepage.
_LINK_HINTS = (
    "about", "company", "who we are", "our story", "careers", "jobs",
    "employment", "openings", "contact", "team",
)

#: Subdomain labels, most likely to carry company/careers evidence first.
SUBDOMAIN_PRIORITY = ("careers", "jobs", "hr", "employment")
SUBDOMAIN_SECONDARY = ("about", "company", "team", "people", "work")

#: Infrastructure subdomains: never useful, never fetched.
SUBDOMAIN_SKIP = (
    "api", "mail", "smtp", "imap", "pop", "ftp", "sftp", "cdn", "static",
    "assets", "img", "images", "media", "dev", "staging", "stage", "test",
    "qa", "uat", "demo", "sandbox", "vpn", "remote", "ns1", "ns2", "mx",
    "autodiscover", "webmail", "owa", "lyncdiscover", "sip", "_domainkey",
    "dkim", "spf", "dmarc", "git", "jenkins", "grafana", "status", "cpanel",
)

_WS = re.compile(r"\s+")


@dataclass
class PageEvidence:
    url: str
    text: str
    via: str  # PRIORITY_PATH | SITEMAP | HOMEPAGE_LINK | HOMEPAGE | SUBDOMAIN


@dataclass
class SiteScanResult:
    pages: list[PageEvidence] = field(default_factory=list)
    pages_fetched: int = 0
    pages_blocked: int = 0
    robots_blocked: int = 0
    used_subdomains: bool = False
    subdomains_discovered: list[str] = field(default_factory=list)
    subdomains_checked: list[str] = field(default_factory=list)


class SiteEvidenceScanner:
    """Fetches a bounded, relevance-ordered set of pages from a company site."""

    def __init__(self, access=None, settings=None, logger=None,
                 respect_robots: bool = True) -> None:
        from nexbase.access.fetcher import AccessLayer
        from nexbase.config import get_settings

        self.settings = settings or get_settings()
        self.access = access or AccessLayer(settings=self.settings)
        self.log = logger or get_logger("nexbase.pipeline.site_evidence")
        self.respect_robots = respect_robots
        self._robots: dict[str, RobotFileParser | None] = {}

    # -- robots ----------------------------------------------------------
    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            self._robots[base] = self._load_robots(base)
        parser = self._robots[base]
        if parser is None:
            return True
        try:
            return parser.can_fetch("NexBaseBot", url)
        except Exception:
            return True

    def _load_robots(self, base: str) -> RobotFileParser | None:
        try:
            page = self.access.fetch(f"{base}/robots.txt")
        except Exception:
            return None
        if not page.html or page.status == 404:
            return None
        parser = RobotFileParser()
        parser.parse(page.html.splitlines())
        return parser

    # -- fetching --------------------------------------------------------
    def _get(self, url: str, result: SiteScanResult) -> str | None:
        if not self.allowed(url):
            result.robots_blocked += 1
            self.log.debug("site_robots_disallowed", url=url)
            return None
        try:
            page = self.access.fetch(url)
        except Exception as exc:
            self.log.debug("site_fetch_error", url=url, error=str(exc))
            return None
        result.pages_fetched += 1
        if not page.ok:
            result.pages_blocked += 1
            return None
        from bs4 import BeautifulSoup

        return _WS.sub(" ", BeautifulSoup(page.html, "html.parser").get_text(" ", strip=True))

    # ------------------------------------------------------------------
    def scan_main_site(
        self, domain: str, budget: int, matcher=None
    ) -> SiteScanResult:
        """Scan the main company website. Stops as soon as ``matcher`` is satisfied."""
        result = SiteScanResult()
        base = f"https://{domain}"

        for url, via in self._main_site_urls(base, budget, result):
            if result.pages_fetched >= budget:
                break
            text = self._get(url, result)
            if not text:
                continue
            result.pages.append(PageEvidence(url, text, via))
            if matcher is not None and matcher(text):
                self.log.info("site_evidence_found", url=url, via=via)
                return result
        return result

    def _main_site_urls(self, base: str, budget: int, result: SiteScanResult):
        """Yield candidate URLs: priority paths, then sitemap, then homepage links."""
        yield base, "HOMEPAGE"
        for path in PRIORITY_PATHS:
            yield urljoin(base, path), "PRIORITY_PATH"

        # Sitemap, if the budget has not already been spent.
        if result.pages_fetched < budget:
            for url in self._sitemap_urls(base, limit=budget):
                yield url, "SITEMAP"

        for url in self._homepage_links(base, result, limit=budget):
            yield url, "HOMEPAGE_LINK"

    def _sitemap_urls(self, base: str, limit: int) -> list[str]:
        """Relevant URLs from sitemap.xml, following one level of sitemap index."""
        found: list[str] = []
        try:
            page = self.access.fetch(urljoin(base, "/sitemap.xml"))
        except Exception:
            return found
        if not page.ok or "<" not in page.html:
            return found

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(page.html, "xml") if "sitemapindex" in page.html[:2000] \
            else BeautifulSoup(page.html, "html.parser")
        locs = [el.get_text(strip=True) for el in soup.find_all("loc")]

        # One level of sitemap index.
        nested = [u for u in locs if u.endswith(".xml")][:2]
        for url in nested:
            try:
                child = self.access.fetch(url)
            except Exception:
                continue
            if child.ok:
                locs += [
                    el.get_text(strip=True)
                    for el in BeautifulSoup(child.html, "html.parser").find_all("loc")
                ]

        for url in locs:
            if url.endswith(".xml"):
                continue
            if any(hint in url.lower() for hint in _LINK_HINTS):
                found.append(url)
            if len(found) >= limit:
                break
        return found

    def _homepage_links(self, base: str, result: SiteScanResult, limit: int) -> list[str]:
        """Same-domain links whose text or href looks relevant."""
        try:
            page = self.access.fetch(base)
        except Exception:
            return []
        if not page.ok:
            return []
        from bs4 import BeautifulSoup

        host = urlsplit(base).netloc
        out: list[str] = []
        for anchor in BeautifulSoup(page.html, "html.parser").select("a[href]"):
            href = urljoin(base, anchor["href"])
            if urlsplit(href).netloc != host:
                continue
            haystack = f"{anchor.get_text(' ', strip=True)} {href}".lower()
            if any(hint in haystack for hint in _LINK_HINTS):
                out.append(href.split("#")[0])
            if len(out) >= limit:
                break
        return list(dict.fromkeys(out))

    # ------------------------------------------------------------------
    # Subdomain fallback
    # ------------------------------------------------------------------
    def discover_subdomains(self, domain: str, limit: int = 25) -> list[str]:
        """Candidate subdomains from Certificate Transparency.

        CT proves a certificate was issued, never that a host is live or
        relevant, so results are candidates only and are validated before use.
        """
        url = f"https://crt.sh/?q=%25.{domain}&output=json"
        try:
            page = self.access.fetch(url)
        except Exception as exc:
            self.log.debug("ct_fetch_error", domain=domain, error=str(exc))
            return []
        if not page.ok:
            return []
        import json

        raw = page.html.strip()
        start = raw.find("[")
        if start == -1:
            return []
        try:
            entries = json.loads(raw[start:])
        except (json.JSONDecodeError, ValueError):
            return []

        names: set[str] = set()
        for entry in entries if isinstance(entries, list) else []:
            for value in str(entry.get("name_value", "")).splitlines():
                host = value.strip().lstrip("*.").lower()
                if host.endswith(f".{domain}") and host != domain:
                    names.add(host)
        return sorted(names)[:limit]

    @staticmethod
    def prioritise_subdomains(hosts: list[str], domain: str) -> list[str]:
        """Business-relevant subdomains first; infrastructure dropped entirely."""
        ranked: list[tuple[int, str]] = []
        for host in hosts:
            label = host[: -(len(domain) + 1)].split(".")[0]
            if label in SUBDOMAIN_SKIP or not label:
                continue
            if label in SUBDOMAIN_PRIORITY:
                ranked.append((0, host))
            elif label in SUBDOMAIN_SECONDARY:
                ranked.append((1, host))
        ranked.sort()
        return [host for _, host in ranked]

    def scan_subdomains(
        self, domain: str, budget: int, matcher=None, company_name: str = ""
    ) -> SiteScanResult:
        """Fallback scan. Only called when the main site yielded nothing."""
        result = SiteScanResult(used_subdomains=True)
        candidates = self.discover_subdomains(domain)
        result.subdomains_discovered = candidates
        relevant = self.prioritise_subdomains(candidates, domain)

        for host in relevant[: self.settings.subdomain_max_checked]:
            if result.pages_fetched >= budget:
                break
            result.subdomains_checked.append(host)
            text = self._get(f"https://{host}", result)
            if not text:
                continue
            result.pages.append(PageEvidence(f"https://{host}", text, "SUBDOMAIN"))
            if matcher is not None and matcher(text):
                self.log.info("subdomain_evidence_found", host=host)
                return result
        return result
