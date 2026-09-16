"""Regression tests for the correctness, discovery, email and security fixes.

Every test here pins a defect that was verified in the repository, so a failure
means the specific defect came back rather than that something merely changed.
No test touches the network or a database.
"""
from __future__ import annotations

import re
from datetime import timedelta

import pytest

from nexbase.access.fetcher import AccessLayer, FetchedPage
from nexbase.access.urlguard import UrlRejected, check_url, is_safe_url
from nexbase.config import Settings
from nexbase.contacts.extraction import extract_emails_from_html, is_role_email
from nexbase.core.enums import QualificationStatus
from nexbase.core.models import RawJob, extract_emails_from_text
from nexbase.discovery import taxonomy
from nexbase.discovery.planner import DiscoveryPlanner
from nexbase.pipeline.company_identity import prepare_companies
from nexbase.pipeline.dedupe import JobDeduplicator
from nexbase.pipeline.normalize import Normalizer, normalize_domain, normalize_location
from nexbase.pipeline.profile import build_profile
from nexbase.pipeline.qualification import QualificationGate, score_company
from nexbase.pipeline.runner import PipelineRunner
from tests.test_pipeline_e2e import StubAccess


def _fresh(jobs, settings, now):
    return prepare_companies(jobs, settings, now)[0]


# ===========================================================================
# Industry: observed evidence wins, inference is only ever a hint
# ===========================================================================
def test_observed_industry_is_never_overwritten_by_title_inference(
    make_job, settings, now
):
    """The defect: an insurer hiring a Claims Adjuster qualified as Manufacturing."""
    assert taxonomy.industry_for_title("Claims Adjuster") == "Manufacturing"

    fresh = _fresh(
        [make_job(company="Zenith Insurance Services", title="Claims Adjuster",
                  industry="Insurance", employees="51 to 200"),
         make_job(company="Zenith Insurance Services", title="Underwriter",
                  industry="Insurance", employees="51 to 200")],
        settings, now,
    )[0]
    profile = build_profile(fresh)

    assert profile.industry == "Insurance"
    assert profile.industry_state == "OBSERVED"
    assert profile.industry_source == "JOB_BOARD"
    # Not force-fitted into one of the client's ten industries.
    assert profile.client_industry is None


def test_production_supervisor_does_not_make_a_company_manufacturing(
    make_job, settings, now
):
    assert taxonomy.industry_for_title("Production Supervisor") is not None

    fresh = _fresh(
        [make_job(company="Midwest Fabrication", title="Production Supervisor",
                  industry=None, description="We are hiring.")],
        settings, now,
    )[0]
    profile = build_profile(fresh)

    assert profile.industry_known is False
    assert profile.industry_state == "INFERRED_HINT"
    assert profile.industry_source == "OCCUPATION_TAXONOMY_HINT"


def test_inferred_industry_earns_no_bonus_and_keeps_the_review_flag(
    make_job, settings, now
):
    fresh = _fresh(
        [make_job(company="Mystery Co", title="Welder", industry=None,
                  employees="51 to 200", description="We are hiring.")],
        settings, now,
    )[0]
    result = score_company(fresh, build_profile(fresh), settings)

    assert result.breakdown["industry_bonus"] == 0.0
    assert "INDUSTRY_UNKNOWN" in result.review_flags
    assert result.status != QualificationStatus.QUALIFIED.value


def test_an_observed_industry_outside_the_ten_is_not_a_rejection(
    make_job, settings, now
):
    """The ten client industries are a target, not an allowlist."""
    fresh = _fresh(
        [make_job(company="Zenith Insurance", title="Adjuster", industry="Insurance",
                  employees="51 to 200", description="Rapidly growing, expanding.")],
        settings, now,
    )[0]
    result = score_company(fresh, build_profile(fresh), settings)

    assert "INDUSTRY_NOT_RELEVANT" not in result.reasons


def test_search_intent_is_still_never_company_evidence(make_job, settings, now):
    job = make_job(company="Some Co", title="Order Picker", industry=None,
                   description="We are hiring.")
    job.search_industry = "Warehousing & Distribution"
    fresh = _fresh([job], settings, now)[0]
    profile = build_profile(fresh)

    assert profile.industry_known is False
    assert profile.industry_source in ("DISCOVERY_INTENT", "OCCUPATION_TAXONOMY_HINT")


# ===========================================================================
# Industry universe: nothing hardcoded as an allowlist, exclusions configurable
# ===========================================================================
def test_any_sector_can_be_selected_for_a_run(settings):
    """No sector is structurally unreachable: the user picks, we run it."""
    from nexbase.discovery.planner import known_sectors

    for sector in known_sectors():
        assert DiscoveryPlanner(settings).plan(sector, "Machine Operator").sector == sector


def test_manufacturing_subsectors_stay_classifiable():
    """Subsector data survives for classification, evidence and suggestions."""
    mfg = [i for i in taxonomy.load_industries() if i.is_manufacturing]
    assert len(mfg) >= 5
    assert taxonomy.suggest_terms_for_sector("Manufacturing", count=5)


# ===========================================================================
# Configuration
# ===========================================================================
def test_size_band_binds_from_the_environment(monkeypatch):
    monkeypatch.setenv("SIZE_FILTER_MIN", "42")
    assert Settings(_env_file=None).size_filter_min == 42


def test_size_band_matches_the_client_spec():
    """Master Plan: <11 reject, 11-200 eligible, >200 reject."""
    s = Settings(_env_file=None)
    assert (s.size_filter_min, s.size_filter_max) == (11, 200)


# ===========================================================================
# Discovery orchestration
# ===========================================================================
def test_one_broken_jobspy_site_does_not_discard_the_others(monkeypatch, settings):
    """ZipRecruiter/Glassdoor/Google fail; Indeed and LinkedIn must survive."""
    import nexbase.discovery.jobspy_discovery as mod

    import pandas as pd

    def fake_scrape(**kwargs):
        site = kwargs["site_name"][0]
        if site in {"zip_recruiter", "glassdoor", "google"}:
            raise RuntimeError(f"{site} is broken upstream")
        return pd.DataFrame([{
            "site": site, "title": "Welder", "company": "Acme",
            "job_url": f"https://{site}.com/j/1", "date_posted": "2026-05-30",
            "location": "Columbus, OH", "description": "d",
        }])

    monkeypatch.setattr(mod, "_scrape_jobs", fake_scrape)
    found = mod.JobSpyDiscovery(settings).search(
        search_terms=["welder"], locations=["Columbus, OH"],
        site_names=["indeed", "zip_recruiter", "linkedin", "glassdoor", "google"],
    )

    assert len(found) == 2
    sites = mod.JobSpyDiscovery(settings)
    assert {j.source_site for j in found} == {"indeed", "linkedin"}


# ===========================================================================
# Data identity
# ===========================================================================
def test_same_name_different_states_do_not_merge(make_job, settings, now):
    """Three unrelated 'Summit Construction' employers became one company."""
    jobs = [
        make_job(company="Summit Construction", title="Carpenter", location="Denver, CO"),
        make_job(company="Summit Construction", title="Electrician", location="Tampa, FL"),
        make_job(company="Summit Construction", title="Laborer", location="Boise, ID"),
    ]
    aggregates = _fresh(jobs, settings, now)

    assert len(aggregates) == 3
    assert all(a.hiring_intensity == 1 for a in aggregates)


def test_same_name_same_state_still_merges_across_sources(make_job, settings, now):
    jobs = [
        make_job(company="Toledo Tool", title="Machinist", location="Toledo, OH",
                 source_site="indeed", external_id="i-1"),
        make_job(company="Toledo Tool", title="Welder", location="Columbus, OH",
                 source_site="talent_com", external_id="t-1"),
    ]
    aggregates = _fresh(jobs, settings, now)

    assert len(aggregates) == 1
    assert aggregates[0].hiring_intensity == 2


def test_distinct_requisitions_are_preserved(make_job):
    """Three real openings collapsed into one and understated hiring intensity."""
    jobs = [make_job(company="Toledo Tool", title="Machinist", location="Toledo, OH",
                     external_id=f"REQ-{n}") for n in (1, 2, 3)]
    normalized, _ = Normalizer().normalize(jobs)
    kept, duplicates = JobDeduplicator().dedupe(normalized)

    assert duplicates == []
    assert {j.external_id for j in kept} == {"REQ-1", "REQ-2", "REQ-3"}


def test_the_same_job_cross_posted_still_collapses(make_job):
    jobs = [
        make_job(company="Toledo Tool", title="Machinist", location="Toledo, OH",
                 source_site="indeed", external_id="indeed-9"),
        make_job(company="Toledo Tool", title="Machinist", location="Toledo, OH",
                 source_site="simplyhired", external_id="sh-4"),
    ]
    normalized, _ = Normalizer().normalize(jobs)
    kept, duplicates = JobDeduplicator().dedupe(normalized)

    assert len(kept) == 1 and len(duplicates) == 1
    assert kept[0].raw.get("cross_posted_on") == ["indeed", "simplyhired"]


@pytest.mark.parametrize(
    "location,expected",
    [("Toledo, OH", "toledo, oh"), ("Columbus, OH 43004", "columbus, oh"),
     ("Denver, Colorado, United States", "denver, co"), ("Remote", "remote"), (None, "")],
)
def test_location_key(location, expected):
    assert normalize_location(location).key == expected


def test_resolved_domain_reaches_contact_discovery(settings, make_job, now):
    """The resolved domain used to be persisted and then used by nothing."""
    from nexbase.pipeline.domain_resolver import Confidence, DomainResult

    class Resolver:
        def resolve(self, company_name, existing_domain=None, source_urls=None,
                    location=None):
            return DomainResult("mysterymfg.com", Confidence.HIGH, "SEARCH",
                                evidence_url="https://mysterymfg.com",
                                signals={"name_affinity": 0.95})

    page = "<html><body><a href='mailto:ceo@mysterymfg.com'>Dana Reed, CEO</a></body></html>"
    access = StubAccess({"mysterymfg.com": page}, settings=settings)
    settings.contacts_for_review_companies = True

    runner = PipelineRunner(settings=settings, repo=None, access=access,
                            domain_resolver=Resolver())
    from nexbase.db.repository import InertRepository

    runner.repo = InertRepository()
    runner.run(
        raw_jobs=[make_job(company="Mystery Mfg", title=t, company_website=None,
                           description="Rapidly growing, expanding, new facility.",
                           external_id=f"m-{t}")
                  for t in ("Machinist", "Welder", "Press Operator")],
        now=now, persist=False,
    )

    assert any("mysterymfg.com" in u for u in access.requested), (
        "contact discovery must visit the resolved domain"
    )


# ===========================================================================
# Email extraction and preservation
# ===========================================================================
def test_ats_description_email_reaches_observed_emails():
    job = RawJob(source_type="ATS", source_priority=1,
                 description="Send your resume to hiring@company.com.")
    assert "hiring@company.com" in job.observed_emails


def test_board_description_email_reaches_observed_emails():
    job = RawJob(source_type="JOB_BOARD", source_priority=2,
                 description="Questions? hiring@company.com")
    assert "hiring@company.com" in job.observed_emails


def test_jobspy_supplied_emails_are_preserved():
    job = RawJob(source_type="JOB_BOARD", source_priority=2,
                 observed_emails=["Careers@Acme.com"],
                 description="Also reach ops@acme.com")
    assert job.observed_emails == ["careers@acme.com", "ops@acme.com"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("a@x.com and A@X.COM", ["a@x.com"]),
        ("one@x.com, two@x.com", ["one@x.com", "two@x.com"]),
        ("mail me at hr@x.com.", ["hr@x.com"]),
        ("logo@2x.png", []),
        ("", []),
        (None, []),
    ],
)
def test_text_email_extraction(text, expected):
    assert extract_emails_from_text(text) == expected


def test_no_email_is_ever_constructed():
    """Nothing may turn a name plus a domain into an address."""
    job = RawJob(source_type="ATS", source_priority=1,
                 company_name="Acme", company_website="https://acme.com",
                 description="Contact Jane Doe, Head of HR, for details.")
    assert job.observed_emails == []


def test_emails_survive_normalization_and_dedup(make_job, settings, now):
    jobs = [
        make_job(company="Acme Manufacturing", title="Welder",
                 description="Apply: hr@acmemfg.com", external_id="a-1"),
        make_job(company="Acme Manufacturing", title="Machinist",
                 description="Apply: plant@acmemfg.com", external_id="a-2"),
    ]
    fresh = _fresh(jobs, settings, now)[0]
    assert set(fresh.company.observed_emails) == {"hr@acmemfg.com", "plant@acmemfg.com"}


@pytest.mark.parametrize(
    "email,is_role",
    [
        ("hrservicecenter@lowes.com", True),
        ("humanresources@acme.com", True),
        ("talent@acme.com", True),
        ("recruiting@acme.com", True),
        ("staffing@acme.com", True),
        ("hiring@acme.com", True),
        ("recruitment@acme.com", True),
        ("careers@acme.com", True),
        ("jane.doe@acme.com", False),
        ("d.reed@acme.com", False),
    ],
)
def test_role_mailbox_classification(email, is_role):
    assert is_role_email(email) is is_role


@pytest.mark.parametrize(
    "html,expected",
    [
        ("<a href='mailto:ops@acme.com'>mail</a>", "ops@acme.com"),
        ("<a href='mailto:ops%40acme.com'>mail</a>", "ops@acme.com"),
        ("<span data-email='ops@acme.com'></span>", "ops@acme.com"),
        ("<p>ops&#64;acme.com</p>", "ops@acme.com"),
        ("<p>jane [at] acme [dot] com</p>", "jane@acme.com"),
    ],
)
def test_explicitly_published_addresses_are_read(html, expected):
    assert expected in extract_emails_from_html(html)


# ===========================================================================
# Access: robots, SSRF, redirects
# ===========================================================================
@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://localhost:8000/admin",
        "http://127.0.0.1/",
        "https://10.0.0.5/",
        "https://192.168.1.10/",
        "https://172.16.0.1/",
        "https://[::1]/",
        "https://acme.local/",
        "file:///etc/passwd",
        "ftp://acme.com/x",
        "https://user:secret@acme.com/",
        "",
    ],
)
def test_ssrf_guard_rejects(url):
    assert is_safe_url(url) is False
    with pytest.raises(UrlRejected):
        check_url(url)


def test_ssrf_guard_allows_a_public_host():
    assert check_url("https://acme.com/about", resolver=lambda h: ["93.184.216.34"])


def test_ssrf_guard_rejects_a_public_name_resolving_to_a_private_address():
    """DNS rebinding: a public-looking host must not smuggle in 127.0.0.1."""
    with pytest.raises(UrlRejected):
        check_url("https://rebind.example/", resolver=lambda h: ["127.0.0.1"])


def test_access_layer_refuses_an_internal_url(settings):
    layer = AccessLayer(settings=settings)
    page = layer.fetch("http://169.254.169.254/latest/meta-data/")
    assert page.blocked
    assert page.error.startswith("URL_REJECTED")
    assert layer.ssrf_blocked == 1


def test_access_layer_honours_robots_for_every_fetch(settings, monkeypatch):
    """robots was only consulted by the optional size resolver, i.e. nowhere."""
    layer = AccessLayer(settings=settings)
    layer._url_resolver = lambda host: ["93.184.216.34"]
    served = []

    class FakePage:
        def __init__(self, html, status=200):
            self.html_content = html
            self.status = status

    def fake_get(url, alternate=False):
        served.append(url)
        if url.endswith("/robots.txt"):
            return FakePage("User-agent: *\nDisallow: /private\n")
        # Long enough to read as a real page. Under the access layer's
        # meaningful-content threshold this escalates to Camoufox, and the
        # test then depends on a real browser reaching the real acme.com.
        return FakePage(
            "<html><head><title>About Acme</title></head><body>"
            + "<p>Acme Fabrication has run its Columbus plant since 1994.</p>" * 20
            + "</body></html>"
        )

    monkeypatch.setattr(layer, "_scrapling_get", fake_get)

    blocked = layer.fetch("https://acme.com/private/team")
    assert blocked.blocked and blocked.error == "ROBOTS_DISALLOWED"
    assert layer.robots_blocked == 1
    assert not any(u.endswith("/private/team") for u in served)

    allowed = layer.fetch("https://acme.com/about")
    assert allowed.ok
    # This test is about robots: an allowed page must be served by Scrapling
    # without escalating to a browser.
    assert layer.fallback_count == 0


def test_robots_is_fetched_once_per_host(settings, monkeypatch):
    layer = AccessLayer(settings=settings)
    layer._url_resolver = lambda host: ["93.184.216.34"]
    robots_hits = []

    class FakePage:
        def __init__(self, html, status=200):
            self.html_content = html
            self.status = status

    def fake_get(url, alternate=False):
        if url.endswith("/robots.txt"):
            robots_hits.append(url)
            return FakePage("User-agent: *\nAllow: /\n")
        # Long enough to read as a real page. Under the access layer's
        # meaningful-content threshold this escalates to Camoufox, and the
        # test then depends on a real browser reaching the real acme.com.
        return FakePage(
            "<html><head><title>About Acme</title></head><body>"
            + "<p>Acme Fabrication has run its Columbus plant since 1994.</p>" * 20
            + "</body></html>"
        )

    monkeypatch.setattr(layer, "_scrapling_get", fake_get)
    for path in ("/a", "/b", "/c"):
        layer.fetch(f"https://acme.com{path}")
    assert len(robots_hits) == 1


# ===========================================================================
# Security: schema, API auth, verification
# ===========================================================================
def test_every_table_has_row_level_security_enabled():
    from pathlib import Path

    sql = Path("nexbase/db/schema.sql").read_text(encoding="utf-8")
    tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql))
    assert tables, "schema must declare tables"

    block = sql[sql.index("ENABLE ROW LEVEL SECURITY") - 2000:]
    for table in tables:
        assert f"'{table}'" in block, f"{table} is not covered by the RLS block"
    assert "CREATE POLICY" not in sql, "no permissive policy may be granted"


def test_api_auth_fails_closed_outside_development(monkeypatch):
    import nexbase.api.main as api
    from fastapi import HTTPException

    monkeypatch.setattr(api, "API_KEY", "")
    monkeypatch.setattr(api.settings, "environment", "production")
    with pytest.raises(HTTPException) as exc:
        api.require_api_key(None)
    assert exc.value.status_code == 503


def test_api_auth_open_only_in_development(monkeypatch):
    import nexbase.api.main as api

    monkeypatch.setattr(api, "API_KEY", "")
    monkeypatch.setattr(api.settings, "environment", "development")
    assert api.require_api_key(None) is None


def test_api_auth_rejects_a_wrong_key(monkeypatch):
    import nexbase.api.main as api
    from fastapi import HTTPException

    monkeypatch.setattr(api, "API_KEY", "correct-horse")
    with pytest.raises(HTTPException) as exc:
        api.require_api_key("wrong")
    assert exc.value.status_code == 401
    assert api.require_api_key("correct-horse") is None


def test_read_endpoints_require_authentication():
    import nexbase.api.main as api

    protected = {
        "/leads", "/pipeline/run", "/companies/{company_id}/emails",
        "/config/sectors",
    }
    seen = set()
    for route in api.app.routes:
        path = getattr(route, "path", None)
        if path not in protected:
            continue
        seen.add(path)
        names = {
            getattr(call, "__name__", "")
            for call in _dependency_callables(route.dependant)
        }
        assert "require_api_key" in names, f"{path} is unauthenticated"
    assert seen == protected, f"missing routes: {sorted(protected - seen)}"


def _dependency_callables(dependant):
    """Every callable in a route's dependency tree, whatever nesting FastAPI used."""
    found = []
    for sub in getattr(dependant, "dependencies", []):
        if getattr(sub, "call", None) is not None:
            found.append(sub.call)
        found.extend(_dependency_callables(sub))
    return found


def test_zerobounce_key_never_travels_in_the_url(monkeypatch, settings):
    import nexbase.email.verification as ver

    settings.zerobounce_api_key = "secret-key"
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "valid"}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers)
        return Response()

    monkeypatch.setattr(ver.httpx, "post", fake_post)
    monkeypatch.setattr(
        ver.httpx, "get",
        lambda *a, **k: pytest.fail("verification must not use a GET query string"),
    )

    ver.EmailVerifier(settings).verify("jane@acme.com")
    assert "secret-key" not in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer secret-key"
    assert captured["json"] == {"email": "jane@acme.com"}


# ===========================================================================
# Operational scope: no recurring cadence, paid verification gated
# ===========================================================================
def test_no_recurring_schedule_is_served_anywhere():
    """A weekday cron used to be served while every document denied it existed."""
    from pathlib import Path

    for path in Path("nexbase").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "cron=" not in source, f"{path} declares a schedule"
        assert ".serve(" not in source, f"{path} serves a flow on a schedule"


def test_page_only_emails_are_recorded_as_company_evidence(settings, make_job, now):
    """A role mailbox is not a person, so it is evidence, not a contact row."""
    from tests.conftest import RecordingRepo

    repo = RecordingRepo()
    page = (
        "<html><body>"
        "<a href='mailto:hrservicecenter@acmemfg.com'>HR</a>"
        "</body></html>"
    )
    runner = PipelineRunner(
        settings=settings, repo=repo,
        access=StubAccess({"acmemfg.com": page}, settings=settings),
    )
    runner.run(
        raw_jobs=[make_job(company="Acme Manufacturing", title=t,
                           company_website="https://acmemfg.com",
                           employees="51 to 200", industry="Industrial Manufacturing",
                           description="Rapidly growing, expanding, new facility.",
                           external_id=f"a-{t}")
                  for t in ("Plant Manager", "Welder", "Machinist")],
        now=now,
    )

    company_emails = [
        row for row in repo.calls.get("evidence", [])
        if row.get("key") == "company_email"
    ]
    assert company_emails, "a page-only address must survive the run"
    row = company_emails[0]
    assert row["value"] == "hrservicecenter@acmemfg.com"
    assert row["raw_payload"]["email_type"] == "ROLE"
    assert row["url"]
    # It is evidence, never a person.
    assert all(
        c.get("email") != "hrservicecenter@acmemfg.com"
        for c in repo.calls.get("contact", [])
    )


def test_pandas_nat_is_not_treated_as_a_posting_date():
    """NaT is a datetime subclass and truthy, so it crashed the live run."""
    import pandas as pd

    from nexbase.core.timeutils import coerce_datetime

    assert coerce_datetime(pd.NaT) is None
    assert coerce_datetime(float("nan")) is None
    assert coerce_datetime(pd.Timestamp("2026-05-30")) is not None

    job = RawJob(source_type="JOB_BOARD", source_priority=2, title="Welder",
                 company_name="Acme", posted_at=None)
    job = RawJob.from_dict({"title": "Welder", "company": "Acme",
                            "date_posted": pd.NaT, "source_type": "JOB_BOARD"})
    assert job.posted_at is None

    normalized, _ = Normalizer().normalize([job])
    assert normalized[0].posting_date is None


# ===========================================================================
# Gap closure: redirects, rate limiting, sitemap contacts, ATS intent, reports
# ===========================================================================
def test_redirect_destination_is_revalidated(settings, monkeypatch):
    """A public host can still redirect to a private address."""
    layer = AccessLayer(settings=settings)
    layer.respect_robots = False
    layer._url_resolver = lambda host: (
        ["127.0.0.1"] if host == "evil.internal.example" else ["93.184.216.34"]
    )

    class FakePage:
        def __init__(self, url):
            self.html_content = (
                "<html><head><title>Internal</title></head><body>"
                + "<p>internal secret content</p>" * 40
                + "</body></html>"
            )
            self.status = 200
            self.url = url

    monkeypatch.setattr(
        layer, "_scrapling_get",
        lambda url, alternate=False: FakePage("https://evil.internal.example/admin"),
    )
    monkeypatch.setattr(
        layer, "_camoufox_fallback",
        lambda url: pytest.fail("must not launch a browser for a rejected redirect"),
    )
    page = layer.fetch("https://acme.com/start")
    assert page.blocked
    assert page.error.startswith("REDIRECT_REJECTED")
    assert page.html == ""
    assert layer.ssrf_blocked == 1


def test_a_same_host_redirect_is_allowed(settings, monkeypatch):
    layer = AccessLayer(settings=settings)
    layer.respect_robots = False
    layer._url_resolver = lambda host: ["93.184.216.34"]

    class FakePage:
        html_content = (
            "<html><head><title>About</title></head><body>"
            # 487 visible characters sat just under the 500-character
            # threshold, so this escalated to Camoufox and fetched the
            # real acme.com.
            + "<p>Acme Fabrication has run its Columbus plant since 1994.</p>" * 20
            + "</body></html>"
        )
        status = 200
        url = "https://acme.com/about/"

    monkeypatch.setattr(layer, "_scrapling_get", lambda url, alternate=False: FakePage())
    assert layer.fetch("https://acme.com/about").ok
    # The redirect check must be settled by Scrapling alone; no browser.
    assert layer.fallback_count == 0


def test_api_rate_limit_rejects_a_flood(monkeypatch):
    import nexbase.api.main as api

    monkeypatch.setattr(api, "RATE_LIMIT_REQUESTS", 3)
    api._rate_hits.clear()

    class Request:
        client = type("C", (), {"host": "198.51.100.7"})()

    request = Request()
    for _ in range(3):
        assert api.rate_limit(request) is None

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        api.rate_limit(request)
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"]


def test_rate_limit_is_per_caller(monkeypatch):
    import nexbase.api.main as api

    monkeypatch.setattr(api, "RATE_LIMIT_REQUESTS", 1)
    api._rate_hits.clear()

    def request(host):
        return type("R", (), {"client": type("C", (), {"host": host})()})()

    assert api.rate_limit(request("198.51.100.1")) is None
    assert api.rate_limit(request("198.51.100.2")) is None


def test_sitemap_contact_pages_stay_on_the_employer_domain(settings):
    from nexbase.contacts.discovery import ContactDiscovery

    sitemap = (
        "<urlset>"
        "<url><loc>https://acme.com/company/leadership-team</loc></url>"
        "<url><loc>https://acme.com/products/widget</loc></url>"
        "<url><loc>https://evil.test/contact</loc></url>"
        "</urlset>"
    )
    access = StubAccess({"acme.com/sitemap.xml": sitemap}, settings=settings)
    picked = ContactDiscovery(access=access, settings=settings)._sitemap_contact_pages(
        "https://acme.com"
    )
    assert picked == ["https://acme.com/company/leadership-team"]


def test_ats_rows_carry_search_intent_not_company_industry():
    """The query's industry is intent; the ATS dataset says nothing about sector."""
    from nexbase.discovery.ats_discovery import ATSDiscovery

    job = ATSDiscovery()._to_raw_job(
        {"title": "Welder", "company": "Acme", "ats_type": "lever"},
        client_industry="Manufacturing",
    )
    assert job.search_industry == "Manufacturing"
    assert job.company_industry is None


def test_pipeline_report_exposes_per_source_status(settings):
    from nexbase.db.repository import InertRepository
    from nexbase.discovery.coverage import SourceOutcome
    from nexbase.discovery.registry import Source, SourceClass, SourceRegistry

    registry = SourceRegistry()
    registry.register(Source("indeed", SourceClass.DIRECT, "fake",
                             lambda q: ([], SourceOutcome(source="x"))))
    plan = DiscoveryPlanner(settings).plan("Manufacturing", "Welder")
    runner = PipelineRunner(settings=settings, repo=InertRepository(),
                            access=StubAccess({}, settings=settings), registry=registry)
    report = runner.run(plan=plan, persist=False)

    assert report.source_status["direct:indeed"]["outcome"] == "EMPTY"
    assert report.source_status["direct:indeed"]["calls"] == len(plan.titles)
    assert "source_status" in report.to_dict()
    assert report.coverage["by_source"]["direct:indeed"]["queries"] == len(plan.titles)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("reach ops@acme.com or OPS@ACME.COM", ["ops@acme.com"]),
        ("a@b.io, c@d.co.uk", ["a@b.io", "c@d.co.uk"]),
        ("not-an-email@", []),
        ("@nodomain.com", []),
        ("version 1.2@3", []),
    ],
)
def test_junk_and_duplicate_addresses(text, expected):
    assert extract_emails_from_text(text) == expected


@pytest.mark.parametrize(
    "prose",
    [
        "<p>join us for a celebration. Learn more today</p>",
        "<p>caring for our patients. Read their stories</p>",
        "<p>the organization to which we belong</p>",
        "<p>we build relationships. Our team delivers</p>",
        "<p>no matter the summer heat</p>",
        "<p>view the latest update. View all</p>",
    ],
)
def test_prose_never_becomes_an_email_address(prose):
    """A live contact-page run turned 'celebration. Learn' into celebr@ion.learn.

    The at/dot decoder accepted a bare " at " and a bare ".", so ordinary words
    containing "at" were read as obfuscated addresses. Fabricating an address is
    strictly worse than missing one.
    """
    assert extract_emails_from_html(prose) == []


@pytest.mark.parametrize(
    "html,expected",
    [
        ("<p>jane [at] acme [dot] com</p>", "jane@acme.com"),
        ("<p>ops (at) acme (dot) com</p>", "ops@acme.com"),
        ("<p>hr {at} acme {dot} co.uk</p>", "hr@acme.co"),
    ],
)
def test_bracketed_obfuscation_is_still_decoded(html, expected):
    assert expected in extract_emails_from_html(html)


# ===========================================================================
# Client spec 2026-09-14: job rules, contact stages, evidence schema
# ===========================================================================
@pytest.mark.parametrize(
    "employment_type,acceptable",
    [
        ("fulltime", True), ("Full-time", True), ("permanent", True),
        (None, True),  # unstated is not a rejection - most boards publish none
        ("contract", False), ("Contract to Hire", False), ("part-time", False),
        ("temporary", False), ("seasonal", False), ("internship", False),
        ("freelance", False), ("per diem", False),
    ],
)
def test_employment_type_rules(employment_type, acceptable):
    from nexbase.pipeline.normalize import employment_verdict

    ok, reason = employment_verdict(employment_type)
    assert ok is acceptable
    assert (reason is None) is acceptable


@pytest.mark.parametrize(
    "country,location,is_us",
    [
        ("us", "Columbus, OH", True), ("USA", "anywhere", True),
        (None, "Columbus, OH", True),
        ("CA", "Toronto", False), (None, "Toronto, Canada", False),
        (None, "London, United Kingdom", False),
    ],
)
def test_us_only_gate(country, location, is_us):
    from nexbase.pipeline.normalize import is_us_location

    assert is_us_location(country, location) is is_us


def test_non_full_time_job_is_rejected_before_age(make_job, settings, now):
    job = make_job(company="Acme Manufacturing", title="Welder", days_old=1)
    job.employment_type = "contract"
    normalized, discarded = Normalizer().normalize([job])
    assert normalized == []
    assert discarded[0].screen_reason.startswith("NOT_FULL_TIME")


def test_non_us_job_is_rejected(make_job, settings, now):
    job = make_job(company="Acme Manufacturing", title="Welder",
                   location="Toronto, Canada", days_old=1)
    job.country = "CA"
    normalized, discarded = Normalizer().normalize([job])
    assert normalized == []
    assert discarded[0].screen_reason == "NOT_US"


def test_evidence_table_defines_every_column_the_code_writes():
    """The runner wrote evidence.raw_payload into a table that lacked it."""
    from pathlib import Path

    sql = Path("nexbase/db/schema.sql").read_text(encoding="utf-8")
    block = sql[sql.index("CREATE TABLE IF NOT EXISTS evidence"):]
    for column in ("record_type", "record_id", "key", "value", "url",
                   "source_type", "source_priority"):
        assert column in block[:900], column
    assert "ADD COLUMN IF NOT EXISTS raw_payload" in sql


def test_call_to_action_text_is_never_a_person(settings):
    """A live run stored "text message" and "email us" as named contacts."""
    from nexbase.contacts.extraction import extract_contacts_from_html

    html = (
        "<div><a href='mailto:info@acme.com'>Email Us</a>"
        "<a href='mailto:sms@acme.com'>Text Message</a>"
        "<a href='mailto:d.reed@acme.com'>Dana Reed, COO</a></div>"
    )
    names = {c.name for c in extract_contacts_from_html(html) if c.name}
    assert "Dana Reed" in names
    assert not {n for n in names if n.lower() in ("email us", "text message")}


def test_contact_discovery_runs_for_review_companies_by_default(settings):
    """Most companies land in NEEDS_REVIEW; skipping them made yield structural."""
    assert settings.contacts_for_review_companies is True


def test_board_adapters_fetch_detail_pages_for_missing_descriptions(settings):
    """SimplyHired publishes no description on its cards, so no email can exist."""
    from nexbase.discovery.board_scrapers import SimplyHiredDiscovery

    detail = (
        "<html><body><div class='description'>"
        "Send your resume to hiring@acmemfg.com to apply."
        "</div></body></html>"
    )
    access = StubAccess({"acmemfg.com/job/1": detail}, settings=settings)
    scraper = SimplyHiredDiscovery(access=access)
    job = RawJob(source_type="JOB_BOARD", source_priority=2, title="Welder",
                 company_name="Acme", description=None,
                 application_url="https://acmemfg.com/job/1")
    scraper._enrich_from_detail_pages([job])

    assert job.description and "hiring@acmemfg.com" in job.description
    assert "hiring@acmemfg.com" in job.observed_emails


def test_detail_page_fetching_is_budgeted(settings):
    from nexbase.discovery.board_scrapers import SimplyHiredDiscovery

    access = StubAccess({}, settings=settings)
    scraper = SimplyHiredDiscovery(access=access)
    scraper.detail_page_budget = 2
    jobs = [RawJob(source_type="JOB_BOARD", source_priority=2, title="W",
                   company_name="A", description=None,
                   application_url=f"https://acme.com/j/{n}") for n in range(10)]
    scraper._enrich_from_detail_pages(jobs)
    assert scraper.details_fetched == 2


def test_contact_discovery_has_a_subdomain_and_directory_stage(settings):
    """Both were missing: subdomain scanning existed only behind a disabled flag."""
    from nexbase.contacts.discovery import ContactDiscovery

    disc = ContactDiscovery(access=StubAccess({}, settings=settings), settings=settings)
    assert hasattr(disc, "_subdomain_contact_pages")
    assert hasattr(disc, "_public_directories")
    assert settings.contacts_scan_subdomains is True
    assert settings.contacts_search_directories is True


def test_an_unlisted_industry_is_not_rejected_for_being_unlisted(
    make_job, settings, now
):
    """A retailer is outside the ten, but that alone must not reject it."""
    fresh = _fresh(
        [make_job(company="Buckeye Retail Group", title="Store Manager",
                  industry="Retail", employees="51 to 200",
                  description="Rapidly growing, expanding, new location.")],
        settings, now,
    )[0]
    result = score_company(fresh, build_profile(fresh), settings)
    assert "INDUSTRY_NOT_TARGETED" not in result.reasons
    assert "INDUSTRY_NOT_RELEVANT" not in result.reasons


def test_manufacturing_subtypes_still_qualify(make_job, settings, now):
    fresh = _fresh(
        [make_job(company="Buckeye Foods", title="Production Supervisor",
                  industry="Food & Beverage Manufacturing", employees="51 to 200",
                  description="Rapidly growing, expanding, new facility.")],
        settings, now,
    )[0]
    result = score_company(fresh, build_profile(fresh), settings)
    assert result.status != QualificationStatus.REJECTED.value


@pytest.mark.parametrize(
    "employment_type,acceptable",
    [
        ("FULL_TIME", True),
        ("PART_TIME", False),          # schema.org spelling; underscores broke matching
        ("PART_TIME, FULL_TIME", True),  # offered full-time, so in scope
        ("CONTRACTOR", False),
        ("TEMPORARY", False),
        ("INTERN", False),
        ("OTHER", True),               # unrecognised is not a rejection
    ],
)
def test_schema_org_employment_types(employment_type, acceptable):
    """SimplyHired publishes schema.org spellings; PART_TIME was slipping through."""
    from nexbase.pipeline.normalize import employment_verdict

    assert employment_verdict(employment_type)[0] is acceptable


def test_detail_page_supplies_date_and_employment_type(settings):
    """SimplyHired cards carry none of these; the posting page carries all three."""
    from nexbase.discovery.board_scrapers import SimplyHiredDiscovery

    detail = """<html><body><script type="application/ld+json">
    {"@type":"JobPosting","datePosted":"2026-08-21T14:49:22Z",
     "employmentType":"FULL_TIME",
     "description":"Apply to hiring@acmemfg.com today."}
    </script></body></html>"""
    access = StubAccess({"acmemfg.com/job/1": detail}, settings=settings)
    scraper = SimplyHiredDiscovery(access=access)
    job = RawJob(source_type="JOB_BOARD", source_priority=2, title="Welder",
                 company_name="Acme", description=None, posted_at=None,
                 application_url="https://acmemfg.com/job/1")
    scraper._enrich_from_detail_pages([job])

    assert job.posted_at is not None and job.posted_at.year == 2026
    assert job.employment_type == "FULL_TIME"
    assert "hiring@acmemfg.com" in job.observed_emails


# ===========================================================================
# Page-fetch losses: 48% of contact pages were never fetched at all
# ===========================================================================
def test_unresolvable_host_is_not_reported_as_a_security_block(settings):
    """Two dead company sites produced 40 URL_REJECTED entries, which read as
    SSRF blocks and hid the real reason in the stats."""
    from nexbase.access.urlguard import HostUnresolved, UrlRejected, check_url

    layer = AccessLayer(settings=settings)
    layer._url_resolver = lambda host: []          # nothing resolves
    page = layer.fetch("https://gone.example/contact")

    assert page.blocked
    assert page.error.startswith("HOST_UNRESOLVED")
    assert layer.unresolved_hosts == 1
    assert layer.ssrf_blocked == 0, "a dead domain is not an attack"

    # Still a refusal: every caller must treat it as one.
    with pytest.raises(UrlRejected):
        check_url("https://gone.example/", resolver=lambda h: [])
    with pytest.raises(HostUnresolved):
        check_url("https://gone.example/", resolver=lambda h: [])


def test_a_private_address_is_still_an_ssrf_block(settings):
    layer = AccessLayer(settings=settings)
    layer._url_resolver = lambda host: ["127.0.0.1"]
    page = layer.fetch("https://rebind.example/")
    assert page.error.startswith("URL_REJECTED")
    assert layer.ssrf_blocked == 1
    assert layer.unresolved_hosts == 0


def test_dead_company_site_is_checked_once_not_once_per_path(settings):
    """Twenty conventional paths were tried against a domain that does not exist."""
    from nexbase.contacts.discovery import ContactDiscovery, ContactDiscoveryReport

    access = StubAccess({}, settings=settings)
    disc = ContactDiscovery(access=access, settings=settings)
    disc._resolved = {}
    import nexbase.contacts.discovery as mod

    original = mod.ContactDiscovery._host_is_live
    mod.ContactDiscovery._host_is_live = lambda self, base: False
    try:
        report = ContactDiscoveryReport(candidates=[])
        found = disc._public_web("Gone Co", "https://gone.example", report)
    finally:
        mod.ContactDiscovery._host_is_live = original

    assert found == []
    assert access.requested == [], "a dead host must cost zero page fetches"


def test_homepage_links_are_preferred_over_guessed_paths(settings):
    """Most of the 20 conventional paths 404. The site names its own real ones."""
    from nexbase.contacts.discovery import ContactDiscovery, ContactDiscoveryReport

    home = (
        "<html><body>"
        "<a href='/who-we-are/our-leadership'>Leadership</a>"
        "<a href='/get-in-touch-contact'>Contact</a>"
        "<a href='/products/widget'>Widget</a>"
        "<a href='https://elsewhere.test/team'>Other host</a>"
        "<a href='mailto:x@acme.com'>Mail</a>"
        "</body></html>"
    )
    access = StubAccess({"acme.com": home}, settings=settings)
    disc = ContactDiscovery(access=access, settings=settings)
    report = ContactDiscoveryReport(candidates=[])
    links = disc._links_from_homepage("https://acme.com", report, "Acme")

    assert "https://acme.com/who-we-are/our-leadership" in links
    assert "https://acme.com/get-in-touch-contact" in links
    assert not any("elsewhere.test" in u for u in links), "never leave the host"
    assert not any("widget" in u for u in links), "only contact-shaped paths"
    assert not any(u.startswith("mailto") for u in links)


def test_homepage_link_harvest_is_bounded(settings):
    from nexbase.contacts.discovery import ContactDiscovery, ContactDiscoveryReport

    home = "<html><body>" + "".join(
        f"<a href='/team-{n}'>T{n}</a>" for n in range(40)
    ) + "</body></html>"
    access = StubAccess({"acme.com": home}, settings=settings)
    disc = ContactDiscovery(access=access, settings=settings)
    links = disc._links_from_homepage(
        "https://acme.com", ContactDiscoveryReport(candidates=[]), "Acme")
    assert len(links) <= settings.contacts_homepage_max_links


def test_form_placeholder_is_never_read_as_an_email():
    """placeholder="email@example.com" accounted for every apparent miss."""
    from nexbase.contacts.extraction import extract_emails_from_html

    html = (
        "<form><input type='text' name='email' "
        "placeholder='email@example.com'></form>"
        "<p>Real: ops@acme.com</p>"
    )
    found = extract_emails_from_html(html)
    assert "ops@acme.com" in found
    assert "email@example.com" not in found


def test_a_transient_dns_failure_does_not_write_off_a_company(settings, monkeypatch):
    """Two live company sites failed to resolve once and resolved minutes later.

    Caching that single failure would skip a real prospect for the whole run.
    """
    import nexbase.contacts.discovery as mod
    from nexbase.contacts.discovery import ContactDiscovery

    settings.contacts_dns_retry_delay_seconds = 0
    calls = []

    def flaky(host):
        calls.append(host)
        return [] if len(calls) == 1 else ["93.184.216.34"]

    monkeypatch.setattr("nexbase.access.urlguard.resolve_host", flaky)
    disc = ContactDiscovery(access=StubAccess({}, settings=settings), settings=settings)
    assert disc._host_is_live("https://acme.com") is True
    assert len(calls) == 2, "the first failure must be retried"


def test_a_genuinely_dead_host_is_cached_after_the_retry(settings, monkeypatch):
    from nexbase.contacts.discovery import ContactDiscovery

    settings.contacts_dns_retry_delay_seconds = 0
    calls = []
    monkeypatch.setattr("nexbase.access.urlguard.resolve_host",
                        lambda host: calls.append(host) or [])
    disc = ContactDiscovery(access=StubAccess({}, settings=settings), settings=settings)

    assert disc._host_is_live("https://gone.example") is False
    assert disc._host_is_live("https://gone.example") is False
    assert len(calls) == 2, "checked twice once, then cached - not per path"


# ===========================================================================
# Lead output: email status, provenance, role vs named
# ===========================================================================
def _emails(contacts=None, extra=None):
    from nexbase.email.discovery import EmailDiscovery

    return EmailDiscovery().discover(contacts or [], extra_emails=extra or [])


def test_email_status_reflects_what_was_observed():
    from nexbase.core.enums import EmailStatus

    assert _emails(extra=["hr@acme.com"]).status == EmailStatus.ROLE_EMAIL_FOUND.value
    assert _emails(
        contacts=[{"email": "d.reed@acme.com", "name": "Dana Reed"}]
    ).status == EmailStatus.PERSONAL_EMAIL_FOUND.value
    assert _emails(
        extra=["hr@acme.com", "d.reed@acme.com"]
    ).status == EmailStatus.MULTIPLE_EMAILS_FOUND.value
    assert _emails().status == EmailStatus.NO_PUBLIC_EMAIL_FOUND.value


def test_observed_emails_carry_provenance():
    result = _emails(contacts=[{
        "email": "d.reed@acme.com", "name": "Dana Reed", "title": "COO",
        "profile_url": "https://acme.com/team", "source_type": "PUBLIC_WEB",
        "discovery_stage": "PUBLIC_WEB",
    }])
    observed = result.observed[0]
    assert observed.source_url == "https://acme.com/team"
    assert observed.contact_name == "Dana Reed" and observed.contact_title == "COO"
    assert observed.kind == "PERSONAL"
    assert observed.verification_status == "PENDING"
    assert result.named and result.named[0].email == "d.reed@acme.com"


def test_role_mailbox_is_never_attributed_to_a_person():
    """Even with a name beside it, a shared mailbox is company evidence."""
    result = _emails(contacts=[{"email": "hr@acme.com", "name": "Dana Reed",
                                "title": "COO"}])
    observed = result.observed[0]
    assert observed.kind == "ROLE"
    assert observed.contact_name is None and observed.contact_title is None
    assert result.named == []
    assert result.role == ["hr@acme.com"]


def test_a_lead_with_only_a_role_mailbox_is_still_a_lead(settings, make_job, now):
    from nexbase.core.enums import EmailStatus
    from nexbase.db.repository import InertRepository

    page = "<html><body><a href='mailto:hr@acmemfg.com'>HR</a></body></html>"
    runner = PipelineRunner(settings=settings, repo=InertRepository(),
                            access=StubAccess({"acmemfg.com": page}, settings=settings))
    report = runner.run(
        raw_jobs=[make_job(company="Acme Manufacturing", title=t,
                           company_website="https://acmemfg.com",
                           employees="51 to 200", industry="Industrial Manufacturing",
                           description="Rapidly growing, expanding, new facility.",
                           external_id=f"a-{t}")
                  for t in ("Plant Manager", "Welder", "Machinist")],
        now=now, persist=False,
    )
    leads = report.qualified + report.needs_review
    assert leads, "the company must not be dropped for lacking a named contact"
    lead = leads[0]
    assert lead.email_status == EmailStatus.ROLE_EMAIL_FOUND.value
    assert "hr@acmemfg.com" in lead.role_mailboxes
    assert lead.named_contact_emails == []


def test_lead_exposes_the_fields_the_output_spec_requires(settings, make_job, now):
    from nexbase.db.repository import InertRepository

    runner = PipelineRunner(settings=settings, repo=InertRepository(),
                            access=StubAccess({}, settings=settings))
    report = runner.run(
        raw_jobs=[make_job(company="Acme Manufacturing", title=t,
                           company_website="https://acmemfg.com",
                           employees="51 to 200", industry="Industrial Manufacturing",
                           description="Rapidly growing, expanding, new facility.",
                           external_id=f"a-{t}")
                  for t in ("Plant Manager", "Welder", "Machinist")],
        now=now, persist=False,
    )
    lead = (report.qualified + report.needs_review)[0]
    for name in ("company_name", "domain", "email_status", "observed_emails",
                 "named_contact_emails", "role_mailboxes", "portals_searched",
                 "discovery_stages", "review_flags", "rejection_reasons",
                 "source_sites", "evidence_url"):
        assert hasattr(lead, name), name


# ===========================================================================
# NEEDS_REVIEW queue
# ===========================================================================
def test_needs_review_company_is_persisted_with_its_flags(settings, make_job, now):
    """Unknown size used to mean the company simply vanished from view."""
    from tests.conftest import RecordingRepo

    repo = RecordingRepo()
    runner = PipelineRunner(settings=settings, repo=repo,
                            access=StubAccess({}, settings=settings))
    report = runner.run(
        raw_jobs=[make_job(company="Mystery Mfg", title=t, employees=None,
                           industry="Industrial Manufacturing",
                           description="Rapidly growing, expanding, new facility.",
                           external_id=f"m-{t}")
                  for t in ("Machinist", "Welder", "Press Operator")],
        now=now,
    )
    assert report.needs_review, "this company must reach review, not disappear"
    row = next(c for c in repo.calls["company"] if c["display_name"] == "Mystery Mfg")
    assert row["qualification_status"] == "NEEDS_REVIEW"
    assert "EMPLOYEE_SIZE_UNKNOWN" in row["review_flags"]
    assert row["hiring_intensity"] == 3


# ===========================================================================
# Operator-selected company size
# ===========================================================================
def test_effective_size_band_matches_the_client_spec():
    """The running config, .env included, must be the Master Plan's 11-200."""
    from nexbase.config import Settings

    live = Settings()
    assert (live.size_filter_min, live.size_filter_max) == (11, 200)


@pytest.mark.parametrize(
    "employees,expected",
    [("5", "UNDERSIZE"), ("51 to 200", "IN_RANGE"),
     ("5,001 to 10,000", "OVERSIZE"), (None, "UNKNOWN")],
)
def test_the_four_size_cases_are_handled_separately(
    settings, make_job, now, employees, expected
):
    from nexbase.pipeline.qualification import evaluate_size

    fresh = _fresh([make_job(company="Acme Manufacturing", title="Welder",
                             employees=employees)], settings, now)[0]
    assert evaluate_size(build_profile(fresh), settings)[0] == expected


def test_a_custom_band_changes_the_verdict_without_touching_evidence(
    settings, make_job, now
):
    """The filter says what we want; it must not rewrite what the source said."""
    from nexbase.pipeline.qualification import evaluate_size

    fresh = _fresh([make_job(company="Acme Manufacturing", title="Welder",
                             employees="51 to 200")], settings, now)[0]
    profile = build_profile(fresh)

    assert evaluate_size(profile, settings)[0] == "IN_RANGE"
    narrow = settings.model_copy(update={"size_filter_min": 1, "size_filter_max": 20})
    assert evaluate_size(profile, narrow)[0] == "OVERSIZE"
    assert (profile.employee_size_min, profile.employee_size_max) == (51, 200)


# ===========================================================================
# Schema statement order
# ===========================================================================
def _schema() -> str:
    from pathlib import Path

    return Path("nexbase/db/schema.sql").read_text(encoding="utf-8")


def test_every_table_is_created_before_it_is_referenced():
    """A trigger loop named review_queue above its CREATE TABLE.

    `DROP TRIGGER IF EXISTS ... ON review_queue` raises when the *table* is
    missing - IF EXISTS covers the trigger, not the table - so the whole script
    aborted and nothing after it was applied.
    """
    sql = _schema()
    created_at = {
        name: sql.index(f"CREATE TABLE IF NOT EXISTS {name}")
        for name in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql)
    }
    assert created_at, "schema must declare tables"

    # Every quoted table name inside a DO block must appear after its CREATE.
    for match in re.finditer(r"FOREACH t IN ARRAY ARRAY\[(.*?)\] LOOP", sql, re.S):
        block_at = match.start()
        for name in re.findall(r"'(\w+)'", match.group(1)):
            assert name in created_at, f"{name} is referenced but never created"
            assert created_at[name] < block_at, (
                f"{name} is used at offset {block_at} but only created at "
                f"{created_at[name]} - the script will abort"
            )


def test_schema_statements_parse_and_are_balanced():
    """Cheap structural check: dollar-quoted DO blocks must be closed."""
    sql = _schema()
    assert sql.count("DO $$") == sql.count("$$;"), "unbalanced DO block"
    assert sql.count("CREATE OR REPLACE FUNCTION") <= sql.count("$$ LANGUAGE plpgsql;")


def test_health_check_covers_every_table_in_the_schema():
    """The health list drifted behind the schema: 15 tables, 13 checked."""
    from pathlib import Path

    # Static comparison: constructing a repository here would open a real
    # connection, because the environment is configured.
    sql = Path("nexbase/db/schema.sql").read_text(encoding="utf-8")
    declared = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql))

    source = Path("nexbase/db/repository.py").read_text(encoding="utf-8")
    block = source[source.index("def health_check"):]
    block = block[block.index("tables = ["):]
    listed = set(re.findall(r'"(\w+)"', block[:block.index("]")]))

    assert declared == listed, (
        f"health check is out of step with the schema: "
        f"missing {sorted(declared - listed)}, extra {sorted(listed - declared)}"
    )


# ===========================================================================
# Phase 1: configurable size band, free size evidence, audit transparency
# ===========================================================================
def test_size_defaults_are_eleven_and_two_hundred():
    s = Settings(_env_file=None)
    assert (s.size_filter_min, s.size_filter_max) == (11, 200)


def test_the_band_stays_configurable():
    """11-200 is the default, not a rule baked into the gate."""
    s = Settings(_env_file=None, size_filter_min=5, size_filter_max=500)
    assert (s.size_filter_min, s.size_filter_max) == (5, 500)


def test_no_size_number_is_hardcoded_in_qualification_logic():
    """The band must come from settings, never from a literal in the gate."""
    from pathlib import Path

    src = Path("nexbase/pipeline/qualification.py").read_text(encoding="utf-8")
    body = "\n".join(
        line for line in src.splitlines()
        if not line.strip().startswith(("#", '"""', "'''", "*", "-"))
    )
    evaluate = body[body.index("def evaluate_size"):body.index("def freshness_bonus")]
    assert "settings.size_filter_max" in evaluate
    assert "settings.size_filter_min" in evaluate
    for literal in ("200", "11", "10"):
        assert f"> {literal}" not in evaluate and f"< {literal}" not in evaluate


def test_cli_run_takes_a_sector_and_a_job_only():
    from nexbase.cli import build_parser

    args = build_parser().parse_args(
        ["run", "--sector", "Manufacturing", "--job", "Welder"])
    assert (args.sector, args.job) == ("Manufacturing", "Welder")
    for removed in (["--min-employees", "25"], ["--term", "x"], ["--location", "OH"]):
        with pytest.raises(SystemExit):
            build_parser().parse_args(
                ["run", "--sector", "Manufacturing", "--job", "Welder", *removed])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--sector", "Manufacturing"])


def test_size_found_on_a_contact_page_is_recorded(settings):
    """Contact discovery reads ~30 pages anyway; an explicit headcount there is free."""
    from nexbase.contacts.discovery import ContactDiscovery, ContactDiscoveryReport

    page = (
        "<html><body><p>Acme Fabrication employs 140 people across two "
        "plants.</p><a href='mailto:hr@acmefab.com'>HR</a></body></html>"
    )
    access = StubAccess({"acmefab.com/about": page}, settings=settings)
    disc = ContactDiscovery(access=access, settings=settings)
    report = ContactDiscoveryReport(candidates=[])
    disc._harvest("https://acmefab.com/about", __import__(
        "nexbase.core.enums", fromlist=["DiscoveryStage"]
    ).DiscoveryStage.PUBLIC_WEB, report, "acme fabrication")

    assert report.size_evidence is not None
    assert report.size_evidence.employee_size_min == 140
    assert report.size_evidence.source == "CONTACT_PAGE"
    assert report.size_evidence.url


def test_a_page_without_a_headcount_records_nothing(settings):
    from nexbase.contacts.discovery import ContactDiscovery, ContactDiscoveryReport
    from nexbase.core.enums import DiscoveryStage

    access = StubAccess({"acmefab.com": "<html><body>We build things.</body></html>"},
                        settings=settings)
    disc = ContactDiscovery(access=access, settings=settings)
    report = ContactDiscoveryReport(candidates=[])
    disc._harvest("https://acmefab.com", DiscoveryStage.PUBLIC_WEB, report, "acme")
    assert report.size_evidence is None


def test_free_size_evidence_clears_review_and_is_persisted(settings, make_job, now):
    """An unknown-size company should not stay in review when a page states it."""
    from tests.conftest import RecordingRepo

    page = (
        "<html><body><p>Acme Manufacturing employs 140 people.</p>"
        "<a href='mailto:d.reed@acmemfg.com'>Dana Reed, COO</a></body></html>"
    )
    repo = RecordingRepo()
    runner = PipelineRunner(
        settings=settings, repo=repo,
        access=StubAccess({"acmemfg.com": page}, settings=settings))
    report = runner.run(
        raw_jobs=[make_job(company="Acme Manufacturing", title=t,
                           company_website="https://acmemfg.com",
                           employees=None, industry="Industrial Manufacturing",
                           description="Rapidly growing, expanding, new facility.",
                           external_id=f"a-{t}")
                  for t in ("Plant Manager", "Welder", "Machinist")],
        now=now,
    )
    lead = (report.qualified + report.needs_review)[0]
    assert lead.employee_size == "140"
    assert lead.employee_size_source == "CONTACT_PAGE"
    assert "EMPLOYEE_SIZE_UNKNOWN" not in lead.review_flags

    sized = [e for e in repo.calls.get("evidence", [])
             if e.get("key") == "employee_size"]
    assert sized, "the evidence must be persisted"
    assert sized[-1]["record_id"] is not None, "evidence must not be orphaned"
    assert sized[-1]["url"]


def test_qualification_reasons_stay_auditable(settings, make_job, now):
    """Every verdict must carry its reasons and the numbers behind them."""
    fresh = _fresh([make_job(company="Acme Manufacturing", title="Welder",
                             employees="51 to 200",
                             industry="Industrial Manufacturing")], settings, now)[0]
    result = score_company(fresh, build_profile(fresh), settings)
    for key in ("size_verdict", "size_known", "employee_size_min",
                "employee_size_max", "industry_state", "industry_source",
                "direct_employer", "total"):
        assert key in result.breakdown, key


# ===========================================================================
# Phase 2: domain resolution budget and ATS match verification
# ===========================================================================
@pytest.mark.parametrize(
    "left,right",
    [
        ("Summit Construction", "Summit Construction Group of Texas"),
        ("Acme Manufacturing Inc", "Acme Manufacturing LLC"),
        ("Toledo Tool", "Toledo Tool and Die"),
    ],
)
def test_name_similarity_accepts_real_variants(left, right):
    from nexbase.pipeline.domain_resolver import name_similarity

    assert name_similarity(left, right) >= 0.9


@pytest.mark.parametrize(
    "left,right",
    [("Summit Construction", "Summit Dental Care"),
     ("Three D Metals", "Metals Unlimited"),
     ("Acme Fabrication", ""),
     ("", "Acme Fabrication")],
)
def test_name_similarity_rejects_different_companies(left, right):
    from nexbase.discovery.ats_discovery import ATSDiscovery
    from nexbase.pipeline.domain_resolver import name_similarity

    assert name_similarity(left, right) < ATSDiscovery.MIN_NAME_SIMILARITY


class _DirectoryClient:
    """Stands in for the ats-scrapers company directory."""

    def __init__(self, rows):
        self.rows = rows

    def find_company(self, name, limit=5):
        import pandas as pd

        return pd.DataFrame(self.rows[:limit])


def _ats_with(rows, settings):
    from nexbase.discovery.ats_discovery import ATSDiscovery

    ats = ATSDiscovery(settings)
    ats._client = _DirectoryClient(rows)
    return ats


def test_ats_rejects_a_same_named_company_that_is_not_a_match(settings):
    """Taking find_company's first hit unverified attached other firms' domains.

    The refusal is recorded rather than silent, so it is auditable.
    """
    ats = _ats_with([{"name": "Summit Dental Care", "url": "https://summitdental.com"}],
                    settings)
    verdict = ats.resolve_company_sites(["Summit Construction"])["Summit Construction"]
    assert verdict["status"] == "NO_MATCH"
    assert verdict["url"] is None


def test_ats_accepts_a_verified_match_with_provenance(settings):
    ats = _ats_with(
        [{"name": "Acme Manufacturing LLC", "url": "https://acmemfg.com",
          "city": "Toledo", "state": "OH"}],
        settings,
    )
    out = ats.resolve_company_sites(["Acme Manufacturing Inc"],
                                    locations={"Acme Manufacturing Inc": "Toledo, OH"})
    match = out["Acme Manufacturing Inc"]
    assert match["status"] == "MATCHED"
    assert match["url"] == "https://acmemfg.com"
    assert match["matched_name"] == "Acme Manufacturing LLC"
    assert match["similarity"] >= 0.75
    assert match["location_match"] is True


def test_ats_prefers_the_location_corroborated_candidate(settings):
    """Two firms share a name; the posting's location decides between them."""
    ats = _ats_with(
        [{"name": "Summit Construction", "url": "https://summit-tx.com",
          "city": "Austin", "state": "TX"},
         {"name": "Summit Construction", "url": "https://summit-oh.com",
          "city": "Toledo", "state": "OH"}],
        settings,
    )
    out = ats.resolve_company_sites(["Summit Construction"],
                                    locations={"Summit Construction": "Toledo, OH"})
    assert out["Summit Construction"]["status"] == "MATCHED"
    assert out["Summit Construction"]["url"] == "https://summit-oh.com"
    assert out["Summit Construction"]["location_match"] is True


def test_ats_match_provenance_reaches_the_job(settings, now):
    from datetime import timedelta

    from nexbase.discovery.registry import attach_ats_company_sites
    from nexbase.logging_setup import get_logger

    job = RawJob(source_type="ATS", source_priority=1, source_site="greenhouse",
                 external_id="gh-1", title="Welder", company_name="Acme Manufacturing",
                 location="Toledo, OH", posted_at=now - timedelta(days=1))

    class FakeATS:
        def __init__(self, *a, **k):
            pass

        def resolve_company_sites(self, names, locations=None):
            assert locations == {"Acme Manufacturing": "Toledo, OH"}
            return {"Acme Manufacturing": {
                "status": "MATCHED", "confidence": "HIGH",
                "url": "https://acmemfg.com", "matched_name": "Acme Manufacturing LLC",
                "similarity": 1.0, "location_match": True,
                "reason": "name and location agree"}}

    attach_ats_company_sites([job], FakeATS(), settings, get_logger())

    assert job.company_website == "https://acmemfg.com"
    assert job.raw["ats_company_match"]["source"] == "ATS_DIRECTORY"
    assert job.raw["ats_company_match"]["name_similarity"] == 1.0


class _RecordingResolver:
    def __init__(self):
        self.order = []

    def resolve(self, company_name, existing_domain=None, source_urls=None,
                location=None):
        from nexbase.pipeline.domain_resolver import Confidence, DomainResult

        self.order.append(company_name)
        return DomainResult(None, Confidence.LOW, "SEARCH")


def test_domain_budget_is_spent_on_the_best_companies_first(settings, make_job, now):
    """The budget is finite, so a QUALIFIED company outranks one in review."""
    from nexbase.db.repository import InertRepository

    settings.domain_resolve_max_per_run = 10
    resolver = _RecordingResolver()
    jobs = []
    for t in ("Plant Manager", "Welder", "Machinist"):
        jobs.append(make_job(company="Good Fit Manufacturing", title=t,
                             employees="51 to 200", industry="Industrial Manufacturing",
                             description="Rapidly growing, expanding, new facility.",
                             external_id=f"g-{t}"))
    for t in ("Machinist", "Welder", "Press Operator"):
        jobs.append(make_job(company="Unknown Size Mfg", title=t, employees=None,
                             industry="Industrial Manufacturing",
                             description="Rapidly growing, expanding, new facility.",
                             external_id=f"u-{t}"))

    runner = PipelineRunner(settings=settings, repo=InertRepository(),
                            access=StubAccess({}, settings=settings),
                            domain_resolver=resolver)
    runner.run(raw_jobs=jobs, now=now, persist=False, stop_at="before_contacts")

    assert resolver.order, "domain resolution must run"
    assert resolver.order[0] == "Good Fit Manufacturing", (
        f"the qualified company must be served first, got {resolver.order}")


def test_domain_budget_reports_what_it_skipped(settings, make_job, now):
    from nexbase.db.repository import InertRepository

    settings.domain_resolve_max_per_run = 1
    jobs = [
        make_job(company=f"Company {n}", title=t, employees=None,
                 description="Rapidly growing, expanding, new facility.",
                 external_id=f"c{n}-{t}")
        for n in range(3) for t in ("Machinist", "Welder", "Press Operator")
    ]
    runner = PipelineRunner(settings=settings, repo=InertRepository(),
                            access=StubAccess({}, settings=settings),
                            domain_resolver=_RecordingResolver())
    report = runner.run(raw_jobs=jobs, now=now, persist=False,
                        stop_at="before_contacts")

    stats = report.domain_resolution
    assert stats["pending"] == 3
    assert stats["candidates"] == 1
    assert stats["skipped_budget"] == 2


def test_domain_budget_default_is_no_longer_a_bottleneck():
    assert Settings(_env_file=None).domain_resolve_max_per_run >= 100


# ===========================================================================
# H5 adversarial: never attach a domain when the match is uncertain
# ===========================================================================
_OH = {"name": "Summit Construction", "url": "https://summit-oh.com",
       "city": "Toledo", "state": "OH"}
_TX = {"name": "Summit Construction", "url": "https://summit-tx.com",
       "city": "Austin", "state": "TX"}
_DENTAL = {"name": "Summit Dental Care", "url": "https://summitdental.com"}
_NOLOC = {"name": "Summit Construction", "url": "https://summit.com"}


def _match(settings, rows, location=None):
    ats = _ats_with(rows, settings)
    out = ats.resolve_company_sites(
        ["Summit Construction"],
        locations={"Summit Construction": location} if location else None)
    return out.get("Summit Construction")


def test_ats_same_name_companies_resolved_by_location(settings):
    """Two real firms share a name; the posting's location decides."""
    verdict = _match(settings, [_OH, _TX], "Toledo, OH")
    assert verdict["status"] == "MATCHED"
    assert verdict["url"] == "https://summit-oh.com"
    assert verdict["confidence"] == "HIGH"


def test_ats_unrelated_fuzzy_match_is_refused(settings):
    verdict = _match(settings, [_DENTAL], "Toledo, OH")
    assert verdict["status"] == "NO_MATCH"
    assert verdict["url"] is None


def test_ats_location_mismatch_is_ambiguous_not_accepted(settings):
    """Same name, contradicting location: a conflict, not a near miss."""
    verdict = _match(settings, [_TX], "Toledo, OH")
    assert verdict["status"] == "AMBIGUOUS"
    assert verdict["url"] is None
    assert "contradict" in verdict["reason"]


def test_ats_single_name_match_with_no_corroboration_is_ambiguous(settings):
    """Policy: the directory search is fuzzy, so a name alone is not enough."""
    verdict = _match(settings, [_NOLOC], None)
    assert verdict["status"] == "AMBIGUOUS"
    assert verdict["url"] is None
    assert verdict["candidate_url"] == "https://summit.com", "kept for review"


def test_ats_multiple_valid_candidates_are_ambiguous(settings):
    rows = [_NOLOC, {"name": "Summit Construction", "url": "https://summit2.com"}]
    verdict = _match(settings, rows, None)
    assert verdict["status"] == "AMBIGUOUS"
    assert verdict["url"] is None


def test_ats_two_location_corroborated_candidates_are_ambiguous(settings):
    rows = [_OH, dict(_OH, url="https://other-oh.com")]
    verdict = _match(settings, rows, "Toledo, OH")
    assert verdict["status"] == "AMBIGUOUS"
    assert verdict["url"] is None


def test_an_ambiguous_ats_match_never_reaches_the_job(settings, now):
    """The whole point: a wrong domain sends contact discovery to another company."""
    from datetime import timedelta

    from nexbase.discovery.registry import attach_ats_company_sites
    from nexbase.logging_setup import get_logger

    job = RawJob(source_type="ATS", source_priority=1, source_site="greenhouse",
                 external_id="gh-1", title="Carpenter",
                 company_name="Summit Construction", location="Toledo, OH",
                 posted_at=now - timedelta(days=1))

    class FakeATS:
        def __init__(self, *a, **k):
            pass

        def resolve_company_sites(self, names, locations=None):
            return {"Summit Construction": {
                "status": "AMBIGUOUS", "url": None, "matched_name": None,
                "similarity": 0.0, "location_match": False,
                "reason": "candidate locations contradict the posting"}}

    attach_ats_company_sites([job], FakeATS(), settings, get_logger())

    assert job.company_website is None, "an uncertain match must not be attached"
    recorded = job.raw["ats_company_match"]
    assert recorded["status"] == "AMBIGUOUS"
    assert recorded["reason"], "the reason must be auditable"


def test_a_rate_limit_does_not_launch_a_browser(settings, monkeypatch):
    """A live benchmark burned 5-7 Camoufox launches per run on Brave 429s.

    Rendering cannot satisfy "you are asking too often"; it only spends the
    fallback budget and the wall clock.
    """
    layer = AccessLayer(settings=settings)
    layer.respect_robots = False
    layer._url_resolver = lambda host: ["93.184.216.34"]

    class FakePage:
        html_content = "<html><body>Too Many Requests</body></html>"
        status = 429
        url = "https://search.brave.com/search?q=x"

    monkeypatch.setattr(layer, "_scrapling_get", lambda url, alternate=False: FakePage())
    monkeypatch.setattr(
        layer, "_camoufox_fallback",
        lambda url: pytest.fail("must not launch a browser for a rate limit"))

    page = layer.fetch("https://search.brave.com/search?q=x")
    assert page.blocked
    assert page.error == "RATE_LIMITED:429"
    assert layer.rate_limited == 1
    assert layer.fallback_count == 0


def test_a_genuine_bot_wall_still_escalates(settings, monkeypatch):
    """403 is a bot wall, and rendering it is exactly what Camoufox is for."""
    layer = AccessLayer(settings=settings)
    layer.respect_robots = False
    layer._url_resolver = lambda host: ["93.184.216.34"]
    launched = []

    class FakePage:
        html_content = "<html><body>Access Denied</body></html>"
        status = 403
        url = "https://acme.com/team"

    monkeypatch.setattr(layer, "_scrapling_get", lambda url, alternate=False: FakePage())
    monkeypatch.setattr(
        layer, "_camoufox_fallback",
        lambda url: launched.append(url) or FetchedPage(
            url, None, "<html>ok</html>", "t", "CAMOUFOX", True))

    layer.fetch("https://acme.com/team")
    assert launched, "a 403 must still escalate"
    assert layer.rate_limited == 0


# ===========================================================================
# ATS location filtering: the planner's "City, ST" matches almost nothing
# ===========================================================================
@pytest.mark.parametrize(
    "location,expected",
    [("Columbus, OH", "OH"), ("Toledo, OH", "OH"), ("Austin, TX", "TX"),
     ("New York, NY", "NY"), ("OH", "OH"),
     # A spelled-out state is narrowed too: the dataset stores the code, so
     # sending "Ohio" matched nothing at all.
     ("Ohio", "OH"), ("Remote", None), (None, None), ("", None)],
)
def test_ats_location_filter_narrows_to_state(location, expected):
    """Measured on the live greenhouse slice for "machinist":

        no location 87 jobs | "OH" 5 | "Ohio" 0 | "Columbus, OH" 0
    """
    from nexbase.discovery.ats_discovery import ats_location_filter

    assert ats_location_filter(location) == expected


def test_ats_source_sends_the_state_code_not_a_place_name(settings, monkeypatch):
    """Passing "Columbus, OH" straight through zeroed the whole ATS channel."""
    import nexbase.discovery.registry as reg
    from nexbase.discovery.planner import NATIONWIDE, US_STATES, TitleVariant

    seen = []

    class FakeATS:
        def __init__(self, *a, **k):
            self.errors = []

        def search(self, **kwargs):
            seen.append(kwargs)
            return []

        def resolve_company_sites(self, names, locations=None):
            return {}

    monkeypatch.setattr(reg, "ATSDiscovery", FakeATS)
    greenhouse = reg.build_registry(settings).get("greenhouse")
    ohio = next(g for g in US_STATES if g.code == "OH")
    for geo in (ohio, NATIONWIDE):
        greenhouse.adapter(reg.SourceQuery(TitleVariant("machinist", "INPUT"), geo,
                                           "Manufacturing", 336))

    assert seen[0]["location"] == "OH"
    assert seen[1]["location"] is None, "nationwide means no location filter"
    assert all(call["query"] == "machinist" and call["ats"] == ["greenhouse"]
               for call in seen)


# ---------------------------------------------------------------------------
# The ATS dataset filters the location by naive substring, so a state code
# leaks. Measured live on the greenhouse slice, 12 of 63 returned rows were in
# the wrong state: "GA" matched "Garden Grove, CA", "Las Vegas, NV" and
# "Mississauga, Ontario, Canada"; "AZ" matched "Frazeysburg, OH"; "CO" matched
# "District of Columbia", "Costa Mesa, CA" and "Scottsdale, AZ".
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "location,expected",
    [("Mason, OH", "OH"),
     ("Warner Robins, Georgia, United States", "GA"),
     ("Columbus, OH \xa0", "OH"),          # trailing non-breaking space
     ("Austin, TX.", "TX"),
     ("Ohio", "OH"),
     ("Washington, District of Columbia, United States", "DC"),
     ("Seattle, Washington", "WA"),
     ("Washington", "WA"),
     ("Mississauga, Ontario, Canada", None),
     ("London, United Kingdom", None),
     ("Remote", None), ("", None), (None, None)],
)
def test_us_state_of_reads_every_shape_the_dataset_stores(location, expected):
    from nexbase.discovery.ats_discovery import us_state_of

    assert us_state_of(location) == expected


@pytest.mark.parametrize(
    "location,state",
    [("Garden Grove, CA", "GA"),
     ("Las Vegas, NV", "GA"),
     ("Mississauga, Ontario, Canada", "GA"),
     ("Frazeysburg, OH, United States", "AZ"),
     ("Washington, District of Columbia, United States", "CO"),
     ("Costa Mesa, California, United States", "CO"),
     ("Scottsdale, Arizona, United States", "CO"),
     ("Columbus, OH \xa0", "CO")],
)
def test_every_measured_substring_leak_is_rejected(location, state):
    """Each pair here was actually returned by the live slice for that state."""
    from nexbase.discovery.ats_discovery import in_us_state

    assert in_us_state(location, state) is False


@pytest.mark.parametrize(
    "location,state",
    [("Mason, OH", "OH"),
     ("Warner Robins, Georgia, United States", "GA"),
     ("Columbus, OH  ", "OH"),
     # Shapes a strict "City, ST" parse threw away. All six were real TX rows.
     ("Fort Worth, TX (North)", "TX"),
     ("12803 West Ave, San Antonio, TX 78216", "TX"),
     ("Lark San Antonio, 15655 Market Hl, San Antonio, TX 78256", "TX"),
     ("Alba, TX; Atlanta, GA; Dallas, TX; Toledo, OH", "TX"),
     ("Texas, United States", "TX")],
)
def test_a_job_in_the_requested_state_survives_the_check(location, state):
    from nexbase.discovery.ats_discovery import in_us_state

    assert in_us_state(location, state) is True


def test_nothing_to_verify_against_keeps_the_row():
    from nexbase.discovery.ats_discovery import in_us_state

    assert in_us_state("Anywhere", None) is True


def test_a_location_that_names_no_state_is_not_assumed_to_match():
    from nexbase.discovery.ats_discovery import in_us_state

    assert in_us_state("Remote", "OH") is False
    assert in_us_state(None, "OH") is False
    # "in" is a word here, not Indiana: the dataset writes codes upper case.
    assert in_us_state("Remote in US", "IN") is False


def test_ats_search_drops_rows_the_dataset_matched_by_substring(settings, monkeypatch):
    """The end-to-end guard: a "CO" search must not return Costa Mesa."""
    import pandas as pd

    import nexbase.discovery.ats_discovery as mod

    rows = [
        {"global_id": "1", "title": "Construction Project Manager",
         "company": "Real CO Builder", "location": "Denver, CO",
         "country_iso": "US", "url": "https://boards.greenhouse.io/a/jobs/1"},
        {"global_id": "2", "title": "Construction Project Manager",
         "company": "Costa Mesa Co", "location": "Costa Mesa, California, United States",
         "country_iso": "US", "url": "https://boards.greenhouse.io/b/jobs/2"},
        {"global_id": "3", "title": "Construction Project Manager",
         "company": "Beltway Co", "location": "Washington, District of Columbia, United States",
         "country_iso": "US", "url": "https://boards.greenhouse.io/c/jobs/3"},
        {"global_id": "4", "title": "Construction Project Manager",
         "company": "Buckeye Co", "location": "Columbus, OH \xa0",
         "country_iso": "US", "url": "https://boards.greenhouse.io/d/jobs/4"},
    ]
    seen = {}

    def fake_search(**kwargs):
        seen.update(kwargs)
        return pd.DataFrame(rows)

    monkeypatch.setattr(mod, "_search_ats", fake_search)

    jobs = mod.ATSDiscovery(settings).search(
        query="construction project manager", location="Denver, CO",
        ats="greenhouse", limit=100)

    # The city was still narrowed to the state before it was sent upstream...
    assert seen["location"] == "CO"
    # ...and the three substring accidents were dropped on the way back.
    assert [j.company_name for j in jobs] == ["Real CO Builder"]


def test_a_slice_that_matched_nothing_is_retried_with_the_state_name(settings, monkeypatch):
    """Slices disagree on the spelling and the filter is case-sensitive.

    Measured for "machinist": greenhouse returns 5 rows for "OH" and 0 for
    "Ohio"; workable returns 0 and 1. Sending only the code silently lost the
    workable row.
    """
    import pandas as pd

    import nexbase.discovery.ats_discovery as mod

    asked = []

    def fake_search(**kwargs):
        asked.append(kwargs["location"])
        if kwargs["location"] == "OH":
            return pd.DataFrame([])
        return pd.DataFrame([
            {"global_id": "1", "title": "Machinist", "company": "Buckeye Tool",
             "location": "Columbus, Ohio, United States", "country_iso": "US",
             "url": "https://apply.workable.com/a/j/1"},
        ])

    monkeypatch.setattr(mod, "_search_ats", fake_search)

    jobs = mod.ATSDiscovery(settings).search(
        query="machinist", location="Columbus, OH", ats="workable", limit=100)

    assert asked == ["OH", "Ohio"], asked
    assert [j.company_name for j in jobs] == ["Buckeye Tool"]


def test_the_state_name_retry_still_verifies_what_comes_back(settings, monkeypatch):
    """The fallback must not become a hole in the state check."""
    import pandas as pd

    import nexbase.discovery.ats_discovery as mod

    def fake_search(**kwargs):
        if kwargs["location"] == "OH":
            return pd.DataFrame([])
        return pd.DataFrame([
            {"global_id": "2", "title": "Machinist", "company": "Elsewhere Inc",
             "location": "Columbus, Georgia, United States", "country_iso": "US",
             "url": "https://apply.workable.com/a/j/2"},
        ])

    monkeypatch.setattr(mod, "_search_ats", fake_search)

    assert mod.ATSDiscovery(settings).search(
        query="machinist", location="Columbus, OH", ats="workable") == []


def test_an_ats_slug_scores_as_the_name_it_was_made_from():
    """The jobs dataset stores the tenant slug, not the employer's name.

    A slug has no word breaks, so it shared no token with the real name and
    every directory match was refused: 10 of 11 live rows came back NO_MATCH.
    """
    from nexbase.pipeline.domain_resolver import name_similarity

    assert name_similarity("componentrepairtechnologies",
                           "Component Repair Technologies") == 1.0
    assert name_similarity("rebuildmanufacturing", "Rebuild Manufacturing") == 1.0
    assert name_similarity("atricure", "AtriCure") == 1.0


def test_squashing_does_not_make_different_employers_match():
    from nexbase.pipeline.domain_resolver import name_similarity

    assert name_similarity("summitconstruction", "Summit Dental") < 0.75
    assert name_similarity("acmemetalfab", "Acme Steel") == 0.0
    assert name_similarity("", "Acme Steel") == 0.0


def test_a_matched_ats_row_adopts_the_employers_real_name(settings, monkeypatch):
    """Everything downstream keys off the name, so the slug must not survive."""
    from nexbase.discovery.registry import attach_ats_company_sites
    from nexbase.logging_setup import get_logger
    from nexbase.core.models import RawJob

    class FakeATS:
        def __init__(self, *a, **k):
            pass

        def resolve_company_sites(self, names, locations=None):
            return {"componentrepairtechnologies": {
                "status": "MATCHED", "confidence": "HIGH",
                "matched_name": "Component Repair Technologies",
                "similarity": 1.0, "location_match": True,
                "url": "https://jobs.lever.co/componentrepairtechnologies",
                "reason": "name and location agree"}}


    job = RawJob(source_type="ATS", source_priority=1, source_site="lever",
                 external_id="1", title="Machinist",
                 company_name="componentrepairtechnologies", location="Mentor, OH")
    attach_ats_company_sites([job], FakeATS(), settings, get_logger())

    assert job.company_name == "Component Repair Technologies"
    assert job.raw["ats_company_slug"] == "componentrepairtechnologies"
    # The directory's URL is the ATS board, not the employer's site.
    assert job.company_website is None


def _directory_row(name, slug, url, location=""):
    return {"name": name, "slug": slug, "url": url, "location": location}


def test_an_exact_tenant_slug_is_an_identifier_not_a_guess(settings, monkeypatch):
    """10 of 11 live rows sat in AMBIGUOUS with nothing left to corroborate them.

    The dataset's company value IS the ATS tenant slug, and the directory
    publishes that same slug, so equality settles identity outright.
    """
    import pandas as pd

    import nexbase.discovery.ats_discovery as mod

    class FakeClient:
        def find_company(self, name, limit=5):
            return pd.DataFrame([_directory_row(
                "Component Repair Technologies", "componentrepairtechnologies",
                "https://jobs.lever.co/componentrepairtechnologies")])

    ats = mod.ATSDiscovery(settings)
    monkeypatch.setattr(ats, "_get_client", lambda: FakeClient())

    verdict = ats.resolve_company_sites(
        ["componentrepairtechnologies"], locations={"componentrepairtechnologies": "Mentor, OH"},
    )["componentrepairtechnologies"]

    assert verdict["status"] == "MATCHED"
    assert verdict["confidence"] == "HIGH"
    assert verdict["matched_name"] == "Component Repair Technologies"


def test_a_different_slug_still_has_to_earn_the_match(settings, monkeypatch):
    """Without the identifier, a lone fuzzy hit stays a review candidate."""
    import pandas as pd

    import nexbase.discovery.ats_discovery as mod

    class FakeClient:
        def find_company(self, name, limit=5):
            return pd.DataFrame([_directory_row(
                "Summit Construction Group", "summitconstructiongroup",
                "https://jobs.lever.co/summitconstructiongroup")])

    ats = mod.ATSDiscovery(settings)
    monkeypatch.setattr(ats, "_get_client", lambda: FakeClient())

    verdict = ats.resolve_company_sites(["summitconstruction"])["summitconstruction"]
    assert verdict["status"] == "AMBIGUOUS"
    assert verdict["url"] is None
