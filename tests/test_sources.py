"""Phase 3: reliable direct, ATS and Apify sources, all emitting the Raw Job schema."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from nexbase.access.fetcher import FetchedPage
from nexbase.core.models import RawJob, raw_job_problems
from nexbase.discovery.coverage import BUDGET_REACHED, ERROR, SOURCE_EXHAUSTED, SourceOutcome
from nexbase.discovery.planner import NATIONWIDE, US_STATES, DiscoveryPlanner, TitleVariant
from nexbase.discovery.registry import (
    JOBSPY_PORTALS,
    Source,
    SourceClass,
    SourceQuery,
    SourceRegistry,
    build_registry,
)
from tests.test_pipeline_e2e import StubAccess

OHIO = next(g for g in US_STATES if g.code == "OH")


def _query(title="Warehouse Manager", geo=NATIONWIDE, hours_old=336):
    return SourceQuery(TitleVariant(title, "INPUT"), geo, "Manufacturing", hours_old)


# ===========================================================================
# The catalog after live verification
# ===========================================================================
def test_only_verified_portals_are_registered(settings):
    registry = build_registry(settings)
    by_class = {}
    for source in registry.sources():
        by_class.setdefault(source.source_class, set()).add(source.portal)

    assert JOBSPY_PORTALS == ("indeed", "linkedin")
    assert by_class[SourceClass.DIRECT] == {
        "indeed", "linkedin", "simplyhired", "talent_com", "postjobfree"}
    assert by_class[SourceClass.APIFY] == {"glassdoor", "zip_recruiter"}
    assert registry.get("google") is None and registry.get("monster") is None


def test_each_source_reports_the_date_window_it_enforces(settings):
    registry = build_registry(settings)
    windows = {s.portal: s.date_window_hours(336) if s.date_window_hours else None
               for s in registry.sources()}
    assert windows["indeed"] == windows["linkedin"] == 336
    assert windows["simplyhired"] == windows["talent_com"] == 336
    assert windows["postjobfree"] is None, "no date filter, so no undated row is trusted"
    assert windows["glassdoor"] == 360 and windows["zip_recruiter"] == 336
    assert all(windows[s.portal] is None for s in registry.sources()
               if s.source_class is SourceClass.ATS)


def test_apify_sources_need_a_token_and_get_their_own_budget(settings):
    for source in build_registry(settings).sources():
        if source.source_class is SourceClass.APIFY:
            assert source.enabled is False
            assert source.disabled_reason == "APIFY_API_TOKEN not set"

    settings.apify_api_token = "token"
    settings.apify_max_calls_per_run = 3
    glassdoor = build_registry(settings).get("glassdoor")
    assert glassdoor.enabled is True and glassdoor.disabled_reason is None
    assert glassdoor.max_calls_per_run == 3


# ===========================================================================
# Raw Job schema
# ===========================================================================
def _raw(**overrides):
    fields = dict(source_type="JOB_BOARD", source_priority=2, source_site="indeed",
                  title="Welder", application_url="https://indeed.com/j/1")
    fields.update(overrides)
    return RawJob(**fields)


def test_a_complete_row_meets_the_schema():
    assert raw_job_problems(_raw(), "indeed") == []
    assert raw_job_problems(_raw(company_name=None, posted_at=None), "indeed") == [], (
        "company and date gaps are judged by normalization and freshness")


@pytest.mark.parametrize("overrides,portal,problem", [
    ({"title": None}, "indeed", "MISSING_TITLE"),
    ({"application_url": None}, "indeed", "MISSING_APPLICATION_URL"),
    ({"source_site": "glassdoor"}, "indeed", "SOURCE_SITE_MISMATCH"),
    ({"source_type": "SCRAPED"}, "indeed", "UNKNOWN_SOURCE_TYPE"),
    ({"posted_at": "2026-09-01"}, "indeed", "POSTED_AT_NOT_DATETIME"),
])
def test_schema_problems_are_named(overrides, portal, problem):
    assert problem in raw_job_problems(_raw(**overrides), portal)


def test_the_registry_drops_rows_that_break_the_schema(settings):
    good = _raw(external_id="1")
    rows = [good, _raw(external_id="2", title=None), _raw(external_id="3", source_site="x"),
            {"title": "not a RawJob"}]
    registry = SourceRegistry()
    registry.register(Source("indeed", SourceClass.DIRECT, "fake",
                             lambda q: (rows, SourceOutcome(source="x", jobs_returned=4))))
    plan = DiscoveryPlanner(settings).plan("Manufacturing", "Zebra Wrangler")

    result = registry.run(plan)

    assert result.jobs == [good]
    status = result.source_status["direct:indeed"]
    assert status["schema_rejected"] == 3
    assert status["schema_problems"] == {
        "MISSING_TITLE": 1, "SOURCE_SITE_MISMATCH": 1, "NOT_A_RAW_JOB": 1}


# ===========================================================================
# Direct boards: SimplyHired, Talent.com, PostJobFree
# ===========================================================================
def _next_data_page(jobs):
    data = {"props": {"pageProps": {"jobs": jobs}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


def test_simplyhired_dates_every_job_from_its_embedded_data():
    from nexbase.discovery.board_scrapers import SimplyHiredDiscovery

    posted = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
    page = _next_data_page([
        {"jobKey": "k1", "title": "Warehouse Manager", "company": "CJ Logistics",
         "location": "Lockbourne, OH", "botUrl": "/job/k1",
         "dateOnIndeed": str(int(posted.timestamp() * 1000)), "jobTypes": ["Full-time"]},
        {"jobKey": "k2", "title": "Shift Lead", "company": "", "location": "Ohio",
         "botUrl": "/job/k2", "jobTypes": []},
        {"jobKey": "k3"},
    ])
    jobs = SimplyHiredDiscovery(access=StubAccess({})).parse(page, "https://x")

    assert [j.title for j in jobs] == ["Warehouse Manager", "Shift Lead"]
    first = jobs[0]
    assert first.posted_at == posted
    assert first.employment_type == "Full-time"
    assert first.application_url == "https://www.simplyhired.com/job/k1"
    assert raw_job_problems(first, "simplyhired") == []
    assert jobs[1].posted_at is None and jobs[1].company_name is None, "nothing invented"


@pytest.mark.parametrize("hours,days", [(None, None), (24, 7), (168, 7), (169, 14), (336, 14), (337, None)])
def test_date_filter_uses_only_verified_windows(hours, days):
    from nexbase.discovery.board_scrapers import date_filter_days

    assert date_filter_days(hours) == days


def test_the_freshness_window_is_pushed_to_boards_that_filter_by_date():
    from nexbase.discovery.board_scrapers import (
        PostJobFreeDiscovery,
        SimplyHiredDiscovery,
        TalentComDiscovery,
    )

    access = StubAccess({})
    assert SimplyHiredDiscovery(access=access).search_url("welder", "Ohio", 1, 336).endswith("&t=14")
    assert TalentComDiscovery(access=access).search_url("welder", "Ohio", 1, 336).endswith("&date=14")
    assert "&" not in PostJobFreeDiscovery(access=access).search_url("welder", "Ohio", 1, 336).split("p=1")[1]
    assert "&t=" not in SimplyHiredDiscovery(access=access).search_url("welder", "Ohio", 1, None)


def _board_with_page(html):
    from nexbase.discovery.board_scrapers import TalentComDiscovery

    scraper = TalentComDiscovery(access=StubAccess({}))
    scraper.access.fetch = lambda url: FetchedPage(url, 200, html, "t", "SCRAPLING", False)
    scraper._enrich_from_detail_pages = lambda found: None
    return scraper


def test_an_empty_first_page_without_the_boards_no_results_text_is_a_parse_failure():
    scraper = _board_with_page("<html><body>a restyled page</body></html>")
    assert scraper.search(["welder"], ["Ohio"]) == []
    outcome = scraper.last_outcomes[0]
    assert (outcome.stop_reason, outcome.error_type) == (ERROR, "PARSE_FAILED")


def test_a_genuine_no_results_page_is_exhausted_not_an_error():
    scraper = _board_with_page("<html><body>No results for welder in Ohio.</body></html>")
    scraper.search(["welder"], ["Ohio"])
    assert scraper.last_outcomes[0].stop_reason == SOURCE_EXHAUSTED


def test_board_adapters_receive_the_plans_freshness_window(settings, monkeypatch):
    import nexbase.discovery.registry as reg

    seen = {}

    class FakeBoard:
        date_window_hours = staticmethod(lambda hours_old: None)

        def __init__(self, access=None, logger=None):
            pass

        def search(self, **kwargs):
            seen.update(kwargs)
            self.last_outcomes = [SourceOutcome(source="x")]
            return []

    monkeypatch.setitem(reg.BOARD_SCRAPERS, "talent_com", FakeBoard)
    build_registry(settings).get("talent_com").adapter(_query(geo=OHIO, hours_old=336))
    assert (seen["hours_old"], seen["locations"], seen["search_terms"]) == (
        336, ["Ohio"], ["Warehouse Manager"])


# ===========================================================================
# ATS: slices on disk, cut to US rows inside the freshness window
# ===========================================================================
def _write_slice(path, now):
    import pandas as pd

    rows = [
        {"global_id": "gh:1", "title": "Welder", "company": "Acme", "ats_type": "greenhouse",
         "location": "Toledo, OH", "country_iso": "US", "url": "https://x/1",
         "posted_at": (now - timedelta(days=2)).isoformat(), "raw": "{}"},
        {"global_id": "gh:2", "title": "Welder", "company": "Stale", "ats_type": "greenhouse",
         "location": "Akron, OH", "country_iso": "US", "url": "https://x/2",
         "posted_at": (now - timedelta(days=40)).isoformat(), "raw": "{}"},
        {"global_id": "gh:3", "title": "Welder", "company": "Undated", "ats_type": "greenhouse",
         "location": "US", "country_iso": "US", "url": "https://x/3", "posted_at": None, "raw": "{}"},
        {"global_id": "gh:4", "title": "Welder", "company": "Abroad", "ats_type": "greenhouse",
         "location": "Berlin", "country_iso": "DE", "url": "https://x/4",
         "posted_at": now.isoformat(), "raw": "{}"},
    ]
    pd.DataFrame(rows).to_parquet(path)
    return len(rows)


def _manifest(rows, sha="a" * 64):
    entry = SimpleNamespace(parquet="https://storage.example/greenhouse/jobs.parquet",
                            sha256=sha, size_bytes=1, rows=rows)
    return SimpleNamespace(by_ats={"greenhouse": entry})


def test_a_cached_slice_is_read_as_us_rows_inside_the_window(tmp_path, monkeypatch):
    import httpx

    from nexbase.discovery.ats_discovery import ATSSliceStore

    now = datetime(2026, 9, 16, tzinfo=timezone.utc)
    rows = _write_slice(tmp_path / f"greenhouse-{'a' * 16}.parquet", now)
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: pytest.fail("cached slice re-downloaded"))

    frame = ATSSliceStore(tmp_path, max_age_days=14).load("greenhouse", _manifest(rows), now=now)

    assert sorted(frame["company"]) == ["Acme", "Undated"]
    assert "raw" not in frame.columns, "unused columns are never read"


def test_mixed_iso_timestamp_formats_are_all_parsed_before_the_window(tmp_path):
    """SmartRecruiters mixes fractional and whole-second timestamps in one slice."""
    import pandas as pd

    from nexbase.discovery.ats_discovery import ATSSliceStore

    now = datetime(2026, 9, 16, tzinfo=timezone.utc)
    base = {"title": "Warehouse Associate", "ats_type": "greenhouse", "location": "OH",
            "country_iso": "US", "url": "https://x", "raw": "{}"}
    rows = [
        {**base, "global_id": "1", "company": "Old", "posted_at": "2023-12-10T00:32:19+00:00"},
        {**base, "global_id": "2", "company": "OldFraction",
         "posted_at": "2026-08-12T03:17:09.378+00:00"},
        {**base, "global_id": "3", "company": "Fresh", "posted_at": "2026-09-15T08:00:00.5+00:00"},
        {**base, "global_id": "4", "company": "FreshNoTz", "posted_at": "2026-09-14T00:00:00"},
    ]
    pd.DataFrame(rows).to_parquet(tmp_path / f"greenhouse-{'a' * 16}.parquet")
    frame = ATSSliceStore(tmp_path, 14).load("greenhouse", _manifest(len(rows)), now=now)
    assert sorted(frame["company"]) == ["Fresh", "FreshNoTz"]


class _Stream:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self, size):
        yield self.body


def test_a_new_slice_version_replaces_the_old_file(tmp_path, monkeypatch):
    import httpx

    from nexbase.discovery.ats_discovery import ATSSliceStore

    source = tmp_path / "source.parquet"
    rows = _write_slice(source, datetime.now(timezone.utc))
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _Stream(source.read_bytes()))
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "greenhouse-oldversion000000.parquet").write_bytes(b"old")

    path = ATSSliceStore(cache, 14).fetch("greenhouse", _manifest(rows, sha="b" * 64))

    assert path.name == f"greenhouse-{'b' * 16}.parquet"
    assert [p.name for p in cache.iterdir()] == [path.name]


def test_a_download_with_the_wrong_row_count_is_discarded(tmp_path, monkeypatch):
    import httpx

    from nexbase.core.errors import DiscoveryError
    from nexbase.discovery.ats_discovery import ATSSliceStore

    source = tmp_path / "source.parquet"
    rows = _write_slice(source, datetime.now(timezone.utc))
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _Stream(source.read_bytes()))
    cache = tmp_path / "cache"

    with pytest.raises(DiscoveryError, match="rows"):
        ATSSliceStore(cache, 14).fetch("greenhouse", _manifest(rows + 1))
    assert not list(cache.iterdir())


def test_ats_queries_use_the_general_result_budget(settings, monkeypatch):
    import nexbase.discovery.registry as reg

    seen = {}

    class FakeATS:
        def __init__(self, *a, **k):
            self.errors = []

        def search(self, **kwargs):
            seen.update(kwargs)
            return []

    monkeypatch.setattr(reg, "ATSDiscovery", FakeATS)
    build_registry(settings).get("lever").adapter(_query())
    assert seen["limit"] == settings.discovery_max_results_per_query


# ===========================================================================
# Apify: Glassdoor and ZipRecruiter
# ===========================================================================
def test_glassdoor_items_map_to_raw_jobs():
    from nexbase.discovery.apify_sources import APIFY_ACTORS

    item = {
        "title": "Warehouse Manager", "company_name": "Acme Foods",
        "platform_url": "https://www.glassdoor.com/job-listing/1",
        "official_url": "https://acmefoods.com/careers/1",
        "posted_date": "2026-09-10", "location": {"raw": "Columbus, OH", "country_code": "US"},
        "description": "<p>Run the <b>warehouse</b>. hr@acmefoods.com</p>",
        "job_type": "full-time", "company_website": "https://acmefoods.com",
        "company_industry": "Food Production", "employee_count": "51 to 200",
        "emails": ["jobs@acmefoods.com"], "applicant_count": 12,
    }
    job = APIFY_ACTORS["glassdoor"].to_raw_job(item, "glassdoor")

    assert raw_job_problems(job, "glassdoor") == []
    assert (job.title, job.company_name, job.location, job.country) == (
        "Warehouse Manager", "Acme Foods", "Columbus, OH", "US")
    assert job.posted_at == datetime(2026, 9, 10)
    assert job.description == "Run the warehouse . hr@acmefoods.com"
    assert (job.company_website, job.company_employee_count, job.applicant_count) == (
        "https://acmefoods.com", "51 to 200", 12)
    assert set(job.observed_emails) == {"jobs@acmefoods.com", "hr@acmefoods.com"}
    assert APIFY_ACTORS["glassdoor"].to_raw_job({"platform_url": "x"}, "glassdoor") is None


def test_ziprecruiter_items_map_to_raw_jobs():
    from nexbase.discovery.apify_sources import APIFY_ACTORS

    item = {"id": "503842941", "url": "https://www.ziprecruiter.com/jobs/503842941",
            "title": "Welder", "company": "Summit Fab", "companyUrl": "https://summitfab.com",
            "location": "Toledo, OH", "country": "US", "employmentType": "FULL_TIME",
            "industry": "Manufacturing", "postedDate": "2026-09-12", "dataType": "job"}
    job = APIFY_ACTORS["zip_recruiter"].to_raw_job(item, "zip_recruiter")

    assert raw_job_problems(job, "zip_recruiter") == []
    assert (job.external_id, job.company_website, job.employment_type, job.company_industry) == (
        "503842941", "https://summitfab.com", "FULL_TIME", "Manufacturing")
    assert job.posted_at == datetime(2026, 9, 12)
    assert APIFY_ACTORS["zip_recruiter"].to_raw_job({**item, "dataType": "summary"}, "zip_recruiter") is None


def test_apify_inputs_follow_the_plan():
    from nexbase.discovery.apify_sources import APIFY_ACTORS

    glassdoor = APIFY_ACTORS["glassdoor"].build_input("Welder", OHIO, 336, 100)
    assert glassdoor["country"] == "United States" and glassdoor["location"] == "Ohio"
    assert glassdoor["max_results"] == 100 and len(glassdoor["posted_since"]) == 10
    assert "location" not in APIFY_ACTORS["glassdoor"].build_input("Welder", NATIONWIDE, 336, 100)

    zip_input = APIFY_ACTORS["zip_recruiter"].build_input("Welder", NATIONWIDE, 336, 50)
    assert (zip_input["searches"], zip_input["postedWithin"], zip_input["maxItems"]) == (
        ["Welder"], "2weeks", 50)
    assert "location" not in zip_input


def test_an_apify_call_is_capped_and_keeps_the_token_out_of_the_url(monkeypatch):
    import httpx

    from nexbase.discovery.apify_sources import run_actor

    sent = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"n": i} for i in range(5)]

    def post(url, **kwargs):
        sent.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr(httpx, "post", post)
    items = run_actor("agentx~glassdoor-jobs-scraper", {"keyword": "x"}, token="secret",
                      max_items=3, max_charge_usd=0.5, timeout_seconds=120)

    assert len(items) == 3
    assert "secret" not in sent["url"] and "secret" not in json.dumps(sent["params"])
    assert sent["headers"]["Authorization"] == "Bearer secret"
    assert (sent["params"]["maxItems"], sent["params"]["maxTotalChargeUsd"]) == (3, 0.5)


def test_the_apify_adapter_reports_a_full_page_as_capped(settings, monkeypatch):
    import nexbase.discovery.registry as reg

    settings.apify_api_token = "token"
    settings.apify_max_items_per_query = 2
    items = [{"id": str(i), "url": f"https://zip/{i}", "title": "Welder", "dataType": "job"}
             for i in range(2)]
    monkeypatch.setattr(reg, "run_actor", lambda *a, **k: items)

    jobs, outcome = build_registry(settings).get("zip_recruiter").adapter(_query())

    assert [j.source_site for j in jobs] == ["zip_recruiter", "zip_recruiter"]
    assert all(j.search_industry == "Manufacturing" for j in jobs)
    assert outcome.stop_reason == BUDGET_REACHED
