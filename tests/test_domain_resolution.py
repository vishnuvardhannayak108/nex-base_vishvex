"""Domain resolution, main-site evidence, subdomain fallback, size filter."""
from __future__ import annotations

import pytest

from nexbase.access.fetcher import FetchedPage
from nexbase.pipeline.domain_resolver import (
    Confidence,
    DomainResolver,
    NullDomainResolver,
    location_tokens,
    name_domain_affinity,
)
from nexbase.pipeline.site_evidence import (
    SUBDOMAIN_SKIP,
    SiteEvidenceScanner,
)
from nexbase.pipeline.size_resolver import WebSizeResolver, extract_employee_size
from tests.test_pipeline_e2e import StubAccess


def _page(body: str, title: str = "Acme Metal Fabrication") -> str:
    return (f"<html><head><title>{title}</title></head><body>{body}"
            + "<p>Serving industrial customers since 1974. </p>" * 30
            + "</body></html>")


ACME_HOME = _page("<h1>Acme Metal Fabrication</h1><p>Columbus, Ohio. Call (614) 555-0100.</p>")
ACME_ABOUT = _page("<p>Acme Metal Fabrication employs 140 people in Columbus, Ohio.</p>")
WRONG_CO = _page("<h1>Zenith Dental Group</h1><p>Dental care in Phoenix.</p>",
                 title="Zenith Dental Group")
SERP = """<html><body>
<a href="/l/?uddg=https%3A%2F%2Fwww.acmemetalfab.com%2F">Acme Metal Fabrication</a>
<a href="/l/?uddg=https%3A%2F%2Fwww.indeed.com%2Fcmp%2FAcme">Acme jobs - Indeed</a>
<a href="/l/?uddg=https%3A%2F%2Fwww.linkedin.com%2Fcompany%2Facme">LinkedIn</a>
</body></html>"""


def _resolver(pages, settings):
    return DomainResolver(access=StubAccess(pages, settings=settings), settings=settings)


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------
def test_existing_domain_takes_precedence(settings):
    r = _resolver({}, settings).resolve("Acme Metal Fabrication",
                                        existing_domain="acmemetalfab.com")
    assert r.domain == "acmemetalfab.com"
    assert r.confidence is Confidence.HIGH
    assert r.source == "EXISTING"


def test_source_url_takes_precedence_over_search(settings):
    r = _resolver({}, settings).resolve(
        "Acme", source_urls=["https://www.acmemetalfab.com/careers"])
    assert r.domain == "acmemetalfab.com"
    assert r.source == "SOURCE_URL"


def test_platform_source_url_is_not_accepted_as_a_domain(settings):
    """An Indeed profile URL identifies the board, not the employer."""
    r = _resolver({}, settings).resolve(
        "Acme", source_urls=["https://www.indeed.com/cmp/Acme"])
    assert r.domain is None


# ---------------------------------------------------------------------------
# Discovery + validation
# ---------------------------------------------------------------------------
def test_successful_discovery_is_high_confidence(settings):
    pages = {"duckduckgo": SERP, "acmemetalfab.com": ACME_HOME}
    r = _resolver(pages, settings).resolve("Acme Metal Fabrication",
                                           location="Columbus, OH")
    assert r.domain == "acmemetalfab.com"
    assert r.confidence is Confidence.HIGH
    assert r.acceptable and r.evidence_url


def test_medium_confidence_when_branding_present_but_location_absent(settings):
    home = _page("<h1>Acme Metal Fabrication</h1>")
    r = _resolver({"duckduckgo": SERP, "acmemetalfab.com": home}, settings).resolve(
        "Acme Metal Fabrication", location="Fresno, CA")
    assert r.domain == "acmemetalfab.com"
    assert r.confidence in (Confidence.HIGH, Confidence.MEDIUM)
    assert r.acceptable


def test_wrong_company_result_is_rejected(settings):
    """The search returned a real site, but it is a different company."""
    serp = '<a href="/l/?uddg=https%3A%2F%2Fzenithdental.com%2F">x</a>'
    r = _resolver({"duckduckgo": serp, "zenithdental.com": WRONG_CO}, settings).resolve(
        "Acme Metal Fabrication", location="Columbus, OH")
    assert r.domain is None
    assert r.confidence is Confidence.LOW


def test_low_confidence_is_never_attached(settings):
    result = _resolver({}, settings).validate("random-host.com", "Acme Metal Fabrication")
    assert result.confidence is Confidence.LOW
    assert result.acceptable is False


def test_blocked_candidate_site_is_low_confidence(settings):
    r = _resolver({"duckduckgo": SERP}, settings).resolve("Acme Metal Fabrication")
    assert r.domain is None


def test_no_search_result_returns_nothing(settings):
    r = _resolver({"duckduckgo": "<html><body>no results</body></html>"},
                  settings).resolve("Acme Metal Fabrication")
    assert r.domain is None
    assert "no candidate" in r.reason


def test_aggregators_are_never_offered_as_candidates(settings):
    candidates = _resolver({"duckduckgo": SERP}, settings)._search("Acme", None)
    assert "indeed.com" not in candidates
    assert "linkedin.com" not in candidates


def test_multiple_candidates_best_one_wins(settings):
    serp = ('<a href="/l/?uddg=https%3A%2F%2Fzenithdental.com%2F">a</a>'
            '<a href="/l/?uddg=https%3A%2F%2Fwww.acmemetalfab.com%2F">b</a>')
    r = _resolver({"duckduckgo": serp, "zenithdental.com": WRONG_CO,
                   "acmemetalfab.com": ACME_HOME}, settings).resolve(
        "Acme Metal Fabrication", location="Columbus, OH")
    assert r.domain == "acmemetalfab.com"


def test_null_resolver_returns_existing_only():
    assert NullDomainResolver().resolve("Acme", existing_domain="acme.com").domain == "acme.com"
    assert NullDomainResolver().resolve("Acme").domain is None


@pytest.mark.parametrize(
    "name,domain,floor",
    [("Acme Metal Fabrication", "acmemetalfab.com", 0.9),
     ("Toledo Tool & Die", "toledotool.com", 0.5),
     ("Zenith Dental Group", "acmemetalfab.com", 0.0)],
)
def test_name_domain_affinity(name, domain, floor):
    assert name_domain_affinity(name, domain) >= floor


def test_location_tokens_expand_state_names():
    assert {"oh", "ohio"} <= location_tokens("Columbus, OH")


# ---------------------------------------------------------------------------
# Main-site evidence
# ---------------------------------------------------------------------------
def _scanner(pages, settings, **kw):
    return SiteEvidenceScanner(access=StubAccess(pages, settings=settings),
                               settings=settings, respect_robots=False, **kw)


_HAS_SIZE = lambda t: extract_employee_size(t) is not None


def test_evidence_found_on_homepage(settings):
    result = _scanner({"acme.com": ACME_ABOUT}, settings).scan_main_site(
        "acme.com", budget=5, matcher=_HAS_SIZE)
    assert result.pages and extract_employee_size(result.pages[-1].text)


def test_evidence_found_on_about_page(settings):
    pages = {"acme.com/about": ACME_ABOUT, "acme.com": ACME_HOME}
    result = _scanner(pages, settings).scan_main_site("acme.com", 6, matcher=_HAS_SIZE)
    assert any(p.url.endswith("/about") for p in result.pages)


def test_evidence_not_found_returns_pages_without_match(settings):
    result = _scanner({"acme.com": ACME_HOME}, settings).scan_main_site(
        "acme.com", 4, matcher=_HAS_SIZE)
    assert all(extract_employee_size(p.text) is None for p in result.pages)


def test_page_budget_is_respected(settings):
    result = _scanner({}, settings).scan_main_site("acme.com", budget=3)
    assert result.pages_fetched <= 3


def test_robots_txt_is_respected(settings):
    pages = {"acme.com/robots.txt": "User-agent: *\nDisallow: /about",
             "acme.com/about": ACME_ABOUT}
    scanner = SiteEvidenceScanner(access=StubAccess(pages, settings=settings),
                                  settings=settings, respect_robots=True)
    result = scanner.scan_main_site("acme.com", 5, matcher=_HAS_SIZE)
    assert result.robots_blocked >= 1
    assert not any(p.url.endswith("/about") for p in result.pages)


# ---------------------------------------------------------------------------
# Subdomain fallback
# ---------------------------------------------------------------------------
CT_JSON = ('[{"name_value":"careers.acme.com"},{"name_value":"api.acme.com"},'
           '{"name_value":"*.acme.com"},{"name_value":"jobs.acme.com"},'
           '{"name_value":"staging.acme.com"}]')


def test_infrastructure_subdomains_are_excluded(settings):
    scanner = _scanner({}, settings)
    hosts = ["api.acme.com", "mail.acme.com", "careers.acme.com", "staging.acme.com"]
    ranked = scanner.prioritise_subdomains(hosts, "acme.com")
    assert ranked == ["careers.acme.com"]
    assert all(s in SUBDOMAIN_SKIP for s in ("api", "mail", "staging"))


def test_careers_and_jobs_are_prioritised(settings):
    ranked = _scanner({}, settings).prioritise_subdomains(
        ["about.acme.com", "jobs.acme.com", "careers.acme.com"], "acme.com")
    assert ranked[:2] == ["careers.acme.com", "jobs.acme.com"]
    assert ranked[-1] == "about.acme.com"


def test_ct_candidates_are_discovered(settings):
    hosts = _scanner({"crt.sh/?q=": CT_JSON}, settings).discover_subdomains("acme.com")
    assert "careers.acme.com" in hosts and "jobs.acme.com" in hosts
    assert "acme.com" not in hosts          # the apex is not a subdomain
    assert not any(h.startswith("*") for h in hosts)


def test_unreachable_subdomain_yields_nothing(settings):
    result = _scanner({"crt.sh/?q=": CT_JSON}, settings).scan_subdomains(
        "acme.com", budget=4, matcher=_HAS_SIZE)
    assert result.used_subdomains
    assert result.pages == []


def test_subdomain_evidence_is_extracted(settings):
    pages = {"crt.sh/?q=": CT_JSON, "careers.acme.com": ACME_ABOUT}
    result = _scanner(pages, settings).scan_subdomains(
        "acme.com", budget=4, matcher=_HAS_SIZE)
    assert result.subdomains_checked
    assert any(extract_employee_size(p.text) for p in result.pages)


# ---------------------------------------------------------------------------
# Main-site-first ordering
# ---------------------------------------------------------------------------
def _size_resolver(pages, settings, **kw):
    return WebSizeResolver(access=StubAccess(pages, settings=settings),
                           settings=settings, respect_robots=False, **kw)


def test_subdomain_fallback_not_triggered_when_main_site_has_evidence(settings):
    resolver = _size_resolver({"acme.com/about": ACME_ABOUT, "acme.com": ACME_HOME,
                               "crt.sh/?q=": CT_JSON}, settings)
    evidence = resolver.resolve("acme.com", "acme")
    assert evidence is not None
    assert evidence.source == "PUBLIC_WEB"
    assert resolver.subdomain_fallbacks == 0, "main site sufficed; must not crawl subdomains"


def test_subdomain_fallback_triggered_only_when_main_site_lacks_evidence(settings):
    resolver = _size_resolver({"acme.com": ACME_HOME, "crt.sh/?q=": CT_JSON,
                               "careers.acme.com": ACME_ABOUT}, settings)
    evidence = resolver.resolve("acme.com", "acme")
    assert resolver.subdomain_fallbacks == 1
    assert evidence is not None
    assert evidence.source == "PUBLIC_WEB_SUBDOMAIN"


def test_subdomain_fallback_can_be_disabled(settings):
    resolver = _size_resolver({"acme.com": ACME_HOME, "crt.sh/?q=": CT_JSON,
                               "careers.acme.com": ACME_ABOUT}, settings,
                              allow_subdomains=False)
    assert resolver.resolve("acme.com", "acme") is None
    assert resolver.subdomain_fallbacks == 0


# ---------------------------------------------------------------------------
# Configurable size filter
# ---------------------------------------------------------------------------
from nexbase.pipeline.qualification import evaluate_size  # noqa: E402
from nexbase.pipeline.profile import CompanyProfile  # noqa: E402


def _profile(lo, hi):
    return CompanyProfile(employee_size_min=lo, employee_size_max=hi, size_known=True)


def test_default_filter_is_the_client_band(settings):
    """Master Plan: <11 reject, 11-200 eligible, >200 reject."""
    assert (settings.size_filter_min, settings.size_filter_max) == (11, 200)
    assert evaluate_size(_profile(10, 10), settings)[0] == "UNDERSIZE"
    assert evaluate_size(_profile(11, 11), settings)[0] == "IN_RANGE"
    assert evaluate_size(_profile(200, 200), settings)[0] == "IN_RANGE"
    assert evaluate_size(_profile(201, 201), settings)[0] == "OVERSIZE"


@pytest.mark.parametrize(
    "lo,hi,size,expected",
    [(50, 500, 300, "IN_RANGE"), (50, 500, 20, "UNDERSIZE"), (50, 500, 900, "OVERSIZE"),
     (1, 50, 20, "IN_RANGE"), (1, 50, 80, "OVERSIZE"),
     (200, 1000, 500, "IN_RANGE"), (200, 1000, 100, "UNDERSIZE"),
     (7, 9999, 5000, "IN_RANGE")],
)
def test_arbitrary_numeric_ranges(settings, lo, hi, size, expected):
    tuned = settings.model_copy(update={"size_filter_min": lo, "size_filter_max": hi})
    assert evaluate_size(_profile(size, size), tuned)[0] == expected


def test_unknown_size_is_never_converted(settings):
    unknown = CompanyProfile()
    assert evaluate_size(unknown, settings)[0] == "UNKNOWN"


def test_observed_size_is_not_the_filter(settings):
    """Changing the filter must not alter recorded evidence about a company."""
    profile = _profile(300, 300)
    wide = settings.model_copy(update={"size_filter_min": 1, "size_filter_max": 1000})
    assert evaluate_size(profile, settings)[0] == "OVERSIZE"
    assert evaluate_size(profile, wide)[0] == "IN_RANGE"
    assert (profile.employee_size_min, profile.employee_size_max) == (300, 300)
