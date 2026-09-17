"""Free / public contact discovery (Master Plan Phase 6), strict order.

1. Same source first   (the qualified job postings' own pages).
2. Other sources       (employer profile pages the sources published).
3. Public web last     (the employer's own site: navigation, conventional
                        team/contact paths, sitemap, subdomains; then public
                        directories when enabled).

Every fetch goes through the Access Layer (SSRF guard, robots.txt, Scrapling ->
Camoufox fallback) and counts against ``CONTACTS_MAX_PAGES_PER_COMPANY``. Every
address seen is kept with the page it was seen on.

Provenance is recorded honestly: a contact scraped off an Indeed employer page
is tagged ``JOB_BOARD``, not ``COMPANY_WEBSITE``. Getting this wrong made the
evidence trail claim a first-party source for a third-party page.
"""
from __future__ import annotations

import time

from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from nexbase.access.fetcher import AccessLayer, FetchedPage
from nexbase.config import Settings, get_settings
from nexbase.contacts.extraction import (
    extract_contacts_from_html,
    extract_emails_from_html,
)
from nexbase.contacts.models import ContactCandidate
from nexbase.core.enums import DiscoveryStage, SourcePriority, SourceType
from nexbase.logging_setup import get_logger

#: Leadership/team paths probed on a company's own website, highest-yield
#: first. The per-host Camoufox cooldown means a JS-heavy site effectively gets
#: one browser render, so the most valuable page must be tried first: named
#: leadership beats a generic "about" blurb.
#: Bounded list of conventional pages. This is not a crawl: only these paths
#: are tried, only on the validated employer domain, and only until enough
#: contacts are found.
COMPANY_SITE_PATHS = (
    "/leadership", "/team", "/our-team", "/management", "/about-us",
    "/about", "/company", "/who-we-are", "/staff", "/contact", "/contact-us",
    "/", "/leadership-team", "/our-people", "/people", "/executive-team",
    "/meet-the-team", "/careers", "/contact/", "/about/leadership",
)

#: Public business directories, tried last. Each publishes company contact
#: pages openly; none requires a login, and each result is still validated the
#: same way a company page is.
DIRECTORY_TEMPLATES = (
    "https://www.manta.com/search?search={q}",
    "https://www.bbb.org/search?find_text={q}",
    "https://opencorporates.com/companies?q={q}",
)

#: Path fragments that mark a sitemap URL as worth opening. Anything else in
#: the sitemap is ignored, so this stays a lookup rather than a crawl.
CONTACT_PATH_TOKENS = (
    "contact", "team", "leadership", "management", "our-people", "people",
    "staff", "about-us", "who-we-are", "executive", "directory",
)

def _source_for_url(url: str) -> tuple[str, int]:
    """Classify a URL's provenance honestly."""
    host = (urlsplit(url).netloc or "").lower()
    board_hosts = (
        "indeed.com", "linkedin.com", "ziprecruiter.com", "glassdoor.com",
        "monster.com", "simplyhired.com", "google.com",
    )
    if any(h in host for h in board_hosts):
        return SourceType.JOB_BOARD.value, int(SourcePriority.JOB_BOARD.value)
    ats_hosts = ("greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
                 "smartrecruiters.com", "workable.com", "bamboohr.com", "icims.com")
    if any(h in host for h in ats_hosts):
        return SourceType.ATS.value, int(SourcePriority.FIRST_PARTY.value)
    return SourceType.COMPANY_WEBSITE.value, int(SourcePriority.PUBLIC_WEB.value)


@dataclass
class ContactDiscoveryReport:
    candidates: list[ContactCandidate]
    pages_fetched: int = 0
    pages_blocked: int = 0
    stages_used: list[str] = field(default_factory=list)
    #: An explicit headcount seen on a page that was fetched for contacts.
    size_evidence: object | None = None
    #: Every address seen on the pages visited, even when it could not be
    #: attributed to a named person, as ``{email, source_url, source_portal,
    #: discovery_stage}``. Observed, never guessed; the first sighting is kept.
    page_emails: list[dict] = field(default_factory=list)
    #: True when the page budget stopped discovery before it was satisfied.
    budget_exhausted: bool = False
    #: Normalized URLs already fetched, so "/leadership" and "/leadership/",
    #: or the homepage read for its links and again as "/", cost one fetch.
    visited: set[str] = field(default_factory=set)

    def first_visit(self, url: str) -> bool:
        parts = urlsplit(url)
        key = (parts.netloc.lower(), parts.path.rstrip("/"), parts.query)
        if key in self.visited:
            return False
        self.visited.add(key)
        return True

    def add_emails(self, emails: list[str], url: str, stage: str) -> None:
        seen = {e["email"] for e in self.page_emails}
        source_type, _ = _source_for_url(url)
        for email in emails:
            if email not in seen:
                seen.add(email)
                self.page_emails.append({"email": email, "source_url": url,
                                         "source_portal": source_type,
                                         "discovery_stage": stage})


class _CountedAccess:
    """Access Layer view that charges every fetch to the company's page budget.

    The sitemap and subdomain helpers fetch through their own scanner; without
    this their fetches were not counted and a company could exceed its budget.
    """

    def __init__(self, access: AccessLayer, report: ContactDiscoveryReport, limit: int) -> None:
        self._access = access
        self._report = report
        self._limit = limit

    def fetch(self, url: str):
        if self._report.pages_fetched >= self._limit:
            self._report.budget_exhausted = True
            return FetchedPage(url=url, status=None, html="", title="", engine="NONE",
                               used_fallback=False, blocked=True,
                               error="PAGE_BUDGET_EXHAUSTED")
        self._report.pages_fetched += 1
        return self._access.fetch(url)

    def __getattr__(self, name):
        return getattr(self._access, name)


class ContactDiscovery:
    """Discovers contacts for a qualified company, honoring stage order."""

    def __init__(
        self,
        access: AccessLayer | None = None,
        settings: Settings | None = None,
        logger=None,
    ) -> None:
        self.access = access or AccessLayer()
        self.settings = settings or get_settings()
        self.log = logger or get_logger("nexbase.contacts.discovery")
        #: host -> resolves. A dead domain is checked once, not once per path.
        self._resolved: dict[str, bool] = {}

    # ------------------------------------------------------------------
    def discover(
        self,
        company_name: str,
        company_website: str | None,
        qualified_job_urls: list[str],
        board_profile_urls: list[str] | None = None,
    ) -> ContactDiscoveryReport:
        """Run the three discovery stages and collect meaningful contacts."""
        report = ContactDiscoveryReport(candidates=[])

        stages = (
            (
                DiscoveryStage.SAME_SOURCE,
                lambda r: self._same_source(company_name, qualified_job_urls, r),
            ),
            (
                DiscoveryStage.OTHER_SOURCE,
                lambda r: self._other_sources(company_name, board_profile_urls or [], r),
            ),
            (
                DiscoveryStage.PUBLIC_WEB,
                lambda r: self._public_web(company_name, company_website, r),
            ),
            # Stage 4: public directories, only once the employer's own site
            # has produced nothing.
            (
                DiscoveryStage.PUBLIC_WEB,
                lambda r: self._public_directories(company_name, company_website, r),
            ),
        )

        for stage, runner in stages:
            found = runner(report)
            if found:
                report.stages_used.append(stage.value)
            report.candidates.extend(found)
            if self._enough(report.candidates):
                self.log.info(
                    "contact_discovery_satisfied",
                    company=company_name,
                    stage=stage.value,
                    contacts=self._meaningful_count(report.candidates),
                )
                break

        self.log.info(
            "contact_discovery_complete",
            company=company_name,
            candidates=len(report.candidates),
            meaningful=self._meaningful_count(report.candidates),
            pages_fetched=report.pages_fetched,
            pages_blocked=report.pages_blocked,
            page_emails=len(report.page_emails),
            stages=report.stages_used,
        )
        return report

    # ------------------------------------------------------------------
    def _enough(self, contacts: list[ContactCandidate]) -> bool:
        return self._meaningful_count(contacts) >= self.settings.contacts_target

    def _budget_left(self, report: ContactDiscoveryReport) -> bool:
        """False once this company has used its page budget."""
        if report.pages_fetched < self.settings.contacts_max_pages_per_company:
            return True
        report.budget_exhausted = True
        return False

    @staticmethod
    def _meaningful_count(contacts: list[ContactCandidate]) -> int:
        seen: set[str] = set()
        for c in contacts:
            if c.meaningful:
                seen.add(c.identity_key)
        return len(seen)

    def _harvest(
        self,
        url: str,
        stage: DiscoveryStage,
        report: ContactDiscoveryReport,
        company_name: str,
    ) -> list[ContactCandidate]:
        if not self._budget_left(report) or not report.first_visit(url):
            return []
        try:
            page = self.access.fetch(url)
        except Exception as exc:
            self.log.warning("contact_fetch_error", url=url, error=str(exc))
            return []

        report.pages_fetched += 1
        self._harvest_size(page, report)
        if not page.ok:
            report.pages_blocked += 1
            self.log.debug(
                "contact_page_blocked", url=url, engine=page.engine, error=page.error
            )
            return []

        report.add_emails(extract_emails_from_html(page.html), page.url, stage.value)

        source_type, source_priority = _source_for_url(page.url)
        found = extract_contacts_from_html(
            page.html,
            base_url=page.url,
            discovery_stage=stage.value,
            source_type=source_type,
            source_priority=source_priority,
        )
        self.log.info(
            "contact_page_scanned",
            company=company_name,
            url=url,
            stage=stage.value,
            engine=page.engine,
            found=len(found),
        )
        return found

    # ------------------------------------------------------------------
    # Stage 1: same source (the original job pages)
    # ------------------------------------------------------------------
    def _same_source(
        self,
        company_name: str,
        job_urls: list[str],
        report: ContactDiscoveryReport,
    ) -> list[ContactCandidate]:
        contacts: list[ContactCandidate] = []
        for url in job_urls[:5]:
            if self._enough(contacts):
                break
            contacts.extend(
                self._harvest(url, DiscoveryStage.SAME_SOURCE, report, company_name)
            )
        return contacts

    # ------------------------------------------------------------------
    # Stage 2: other sources (employer profile pages on other portals)
    # ------------------------------------------------------------------
    def _other_sources(
        self,
        company_name: str,
        board_profile_urls: list[str],
        report: ContactDiscoveryReport,
    ) -> list[ContactCandidate]:
        """Employer profile pages the sources themselves published.

        Only URLs a posting carried are used. A profile URL guessed from the
        company name can belong to a different employer with the same name.
        """
        contacts: list[ContactCandidate] = []
        urls: list[str] = list(dict.fromkeys(board_profile_urls))

        for url in urls[:4]:
            if self._enough(contacts):
                break
            contacts.extend(
                self._harvest(url, DiscoveryStage.OTHER_SOURCE, report, company_name)
            )

        if not contacts:
            self.log.info("contact_other_sources_empty", company=company_name)
        return contacts

    # ------------------------------------------------------------------
    # Stage 3: public web (company leadership/team pages)
    # ------------------------------------------------------------------
    def _public_web(
        self,
        company_name: str,
        company_website: str | None,
        report: ContactDiscoveryReport,
    ) -> list[ContactCandidate]:
        if not company_website:
            self.log.info(
                "contact_public_web_skipped", company=company_name, reason="NO_COMPANY_WEBSITE"
            )
            return []

        base = company_website if "//" in company_website else f"https://{company_website}"

        # One resolution check for the whole company. A dead domain used to
        # cost twenty identical failures.
        if not self._host_is_live(base):
            self.log.info("contact_public_web_skipped", company=company_name,
                          reason="HOST_UNRESOLVED", website=base)
            return []

        contacts: list[ContactCandidate] = []

        # The site's own navigation names its real contact and team pages, so
        # try those before guessing at conventional paths. One fetch, and it
        # removes most of the 404s the blind list produced.
        for url in self._links_from_homepage(base, report, company_name):
            if self._enough(contacts):
                break
            contacts.extend(
                self._harvest(url, DiscoveryStage.PUBLIC_WEB, report, company_name)
            )

        for path in COMPANY_SITE_PATHS:
            if self._enough(contacts):
                break
            contacts.extend(
                self._harvest(
                    urljoin(base, path), DiscoveryStage.PUBLIC_WEB, report, company_name
                )
            )

        # Conventional paths cover most sites; the rest publish their people
        # page somewhere unguessable. The sitemap names it, so this reads the
        # sitemap rather than crawling - only URLs whose path already looks like
        # a team/contact page are fetched, and only from this same host.
        if not self._enough(contacts) and self._budget_left(report):
            for url in self._sitemap_contact_pages(base, report):
                if self._enough(contacts):
                    break
                contacts.extend(
                    self._harvest(url, DiscoveryStage.PUBLIC_WEB, report, company_name)
                )

        # Employers routinely put their people on a subdomain rather than the
        # apex: careers.acme.com, hr.acme.com. Candidates come from Certificate
        # Transparency and are ranked before any is fetched, so this stays a
        # short list of likely hosts, not a sweep.
        if not self._enough(contacts) and self._budget_left(report):
            for url in self._subdomain_contact_pages(base, report):
                if self._enough(contacts):
                    break
                contacts.extend(
                    self._harvest(url, DiscoveryStage.PUBLIC_WEB, report, company_name)
                )
        return contacts

    def _public_directories(
        self, company_name: str, company_website: str | None, report
    ) -> list:
        """Stage 4: look the company up in public business directories."""
        from nexbase.pipeline.normalize import extract_host, registrable_domain

        domain = registrable_domain(extract_host(company_website or "")) or None
        contacts = []
        for url in self._directory_pages(company_name, domain):
            if self._enough(contacts):
                break
            contacts.extend(
                self._harvest(url, DiscoveryStage.PUBLIC_WEB, report, company_name)
            )
        return contacts

    def _subdomain_contact_pages(self, base: str, report: ContactDiscoveryReport) -> list[str]:
        """Contact pages on the employer's own subdomains, bounded and ranked."""
        if not self.settings.contacts_scan_subdomains:
            return []
        from nexbase.pipeline.normalize import extract_host, registrable_domain

        domain = registrable_domain(extract_host(base))
        if not domain:
            return []
        try:
            from nexbase.pipeline.site_evidence import SiteEvidenceScanner

            scanner = SiteEvidenceScanner(
                access=_CountedAccess(self.access, report,
                                      self.settings.contacts_max_pages_per_company),
                settings=self.settings,
                logger=self.log,
            )
            hosts = scanner.prioritise_subdomains(
                scanner.discover_subdomains(domain), domain
            )
        except Exception as exc:
            self.log.debug("contact_subdomain_error", domain=domain, error=str(exc))
            return []

        urls: list[str] = []
        for host in hosts[: self.settings.contacts_subdomain_max_hosts]:
            urls += [f"https://{host}{path}"
                     for path in ("/contact", "/team", "/about")]
        if urls:
            self.log.info("contact_subdomains", domain=domain, hosts=len(hosts))
        return urls

    def _directory_pages(self, company_name: str, domain: str | None) -> list[str]:
        """Public business directories, searched by name.

        The fourth stage: used only when the employer's own site yielded
        nothing. These are public listing pages; nothing behind a login is
        touched, and every address found is still an observed one.
        """
        if not self.settings.contacts_search_directories:
            return []
        from urllib.parse import quote_plus

        query = quote_plus(f"{company_name} {domain or ''}".strip())
        return [t.format(q=query) for t in DIRECTORY_TEMPLATES]

    def _harvest_size(self, page, report) -> None:
        """Record an explicit employee count if this page happens to state one.

        Free evidence: the page was fetched for contacts regardless. The first
        explicit statement wins, so a later marketing page cannot overwrite an
        about-page fact.
        """
        if report.size_evidence is not None or not getattr(page, "ok", False):
            return
        from bs4 import BeautifulSoup

        from nexbase.pipeline.size_resolver import SizeEvidence, extract_employee_size

        try:
            text = BeautifulSoup(page.html, "html.parser").get_text(" ", strip=True)
        except Exception:
            return
        found = extract_employee_size(text)
        if not found:
            return
        low, high, snippet = found
        report.size_evidence = SizeEvidence(
            employee_size_min=low, employee_size_max=high,
            source="CONTACT_PAGE", url=page.url, snippet=snippet,
        )
        self.log.info("size_found_during_contact_discovery",
                      url=page.url, low=low, high=high)

    def _host_is_live(self, base: str) -> bool:
        """Resolve the company's host once. False means the website is gone."""
        from nexbase.access.urlguard import resolve_host
        from nexbase.pipeline.normalize import extract_host

        host = extract_host(base)
        if not host:
            return False
        if host in self._resolved:
            return self._resolved[host]

        # Retry once before writing a company off. DNS here is intermittent:
        # two live company sites failed to resolve during one audit and
        # resolved normally minutes later. Caching a single failed lookup would
        # skip a real prospect for the whole run - worse than the per-path
        # lookups this replaced.
        live = bool(resolve_host(host))
        if not live:
            time.sleep(self.settings.contacts_dns_retry_delay_seconds)
            live = bool(resolve_host(host))
            if not live:
                self.log.info("contact_host_unresolved", host=host)
        self._resolved[host] = live
        return live

    def _links_from_homepage(self, base: str, report, company_name: str) -> list[str]:
        """Contact/team URLs the homepage itself links to, same host only."""
        from urllib.parse import urljoin as _join

        from bs4 import BeautifulSoup

        from nexbase.pipeline.normalize import extract_host

        if not self._budget_left(report) or not report.first_visit(base):
            return []
        try:
            page = self.access.fetch(base)
        except Exception:
            return []
        report.pages_fetched += 1
        if not page.ok:
            report.pages_blocked += 1
            return []
        report.add_emails(extract_emails_from_html(page.html), page.url,
                          DiscoveryStage.PUBLIC_WEB.value)

        host = extract_host(base)
        seen: dict[str, None] = {}
        for anchor in BeautifulSoup(page.html, "html.parser").select("a[href]"):
            href = (anchor.get("href") or "").strip()
            if not href or href.startswith(("mailto:", "tel:", "#", "javascript:")):
                continue
            url = _join(base, href)
            if extract_host(url) != host:
                continue
            if any(token in url.lower() for token in CONTACT_PATH_TOKENS):
                seen.setdefault(url.split("#")[0], None)
            if len(seen) >= self.settings.contacts_homepage_max_links:
                break
        if seen:
            self.log.info("contact_homepage_links", company=company_name,
                          found=len(seen))
        return list(seen)

    def _sitemap_contact_pages(self, base: str, report: ContactDiscoveryReport) -> list[str]:
        """Contact/team URLs named by the site's own sitemap, bounded."""
        from nexbase.pipeline.normalize import extract_host

        try:
            from nexbase.pipeline.site_evidence import SiteEvidenceScanner

            scanner = SiteEvidenceScanner(
                access=_CountedAccess(self.access, report,
                                      self.settings.contacts_max_pages_per_company),
                settings=self.settings,
                logger=self.log,
            )
            candidates = scanner._sitemap_urls(
                base.rstrip("/"), self.settings.contacts_sitemap_max_urls
            )
        except Exception as exc:
            self.log.debug("contact_sitemap_error", base=base, error=str(exc))
            return []

        host = extract_host(base)
        picked: list[str] = []
        for url in candidates:
            if extract_host(url) != host:
                continue  # never leave the validated employer domain
            if any(token in url.lower() for token in CONTACT_PATH_TOKENS):
                picked.append(url)
            if len(picked) >= self.settings.contacts_sitemap_max_pages:
                break
        if picked:
            self.log.info("contact_sitemap_pages", base=base, found=len(picked))
        return picked
