"""Source adapters report why they stopped, and stay inside the USA.

**Coverage, not the first N.** Sources are paged until they run out or refuse,
and when *we* stop a source early that is recorded as our decision, not mistaken
for the source having nothing left. Planning and the registry are covered in
``test_planner_registry.py``.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from nexbase.config import Settings
from nexbase.discovery.coverage import (
    BLOCKED,
    BUDGET_REACHED,
    ROBOTS_RESTRICTED,
    SOURCE_EXHAUSTED,
    CoverageReport,
    SourceOutcome,
    classify_fetch_failure,
)
from tests.test_hardening import StubAccess


# ===========================================================================
# Coverage: maximum practical, with our limits told apart from theirs
# ===========================================================================
def test_board_pagination_continues_until_the_source_is_exhausted(settings):
    """Three pages of rows, then an empty one: all three pages are taken."""
    from nexbase.discovery.board_scrapers import SimplyHiredDiscovery

    pages = {1: 5, 2: 5, 3: 2, 4: 0}
    seen_pages = []

    scraper = SimplyHiredDiscovery(access=StubAccess({}, settings=settings))
    scraper.access.fetch = lambda url: _page(url)
    scraper.parse = lambda html, url, client_industry=None: _rows(
        int(url.rsplit("pn=", 1)[-1]), pages, seen_pages)
    scraper._enrich_from_detail_pages = lambda found: None

    jobs = scraper.search(search_terms=["welder"], locations=["USA"], pages=25)

    assert seen_pages == [1, 2, 3, 4]
    assert len(jobs) == 12
    assert scraper.last_outcomes[0].stop_reason == SOURCE_EXHAUSTED
    assert scraper.last_outcomes[0].limited_by_source is True


def test_a_board_stopped_by_our_budget_says_so(settings):
    """Every page still had new rows, so the board had more and we stopped."""
    from nexbase.discovery.board_scrapers import SimplyHiredDiscovery

    seen_pages = []
    scraper = SimplyHiredDiscovery(access=StubAccess({}, settings=settings))
    scraper.access.fetch = lambda url: _page(url)
    scraper.parse = lambda html, url, client_industry=None: _rows(
        int(url.rsplit("pn=", 1)[-1]), {}, seen_pages, default=5)
    scraper._enrich_from_detail_pages = lambda found: None

    scraper.search(search_terms=["welder"], locations=["USA"], pages=3)

    outcome = scraper.last_outcomes[0]
    assert seen_pages == [1, 2, 3]
    assert outcome.stop_reason == BUDGET_REACHED
    assert outcome.limited_by_nexbase is True
    assert outcome.limited_by_source is False


def test_a_blocked_board_is_not_recorded_as_empty(settings):
    """Cloudflare must never look like "no jobs exist"."""
    from nexbase.access.fetcher import FetchedPage
    from nexbase.discovery.board_scrapers import SimplyHiredDiscovery

    scraper = SimplyHiredDiscovery(access=StubAccess({}, settings=settings))
    scraper.access.fetch = lambda url: FetchedPage(
        url=url, status=403, html="", title="", engine="SCRAPLING",
        used_fallback=False, blocked=True, error="BLOCKED_BY_CLOUDFLARE")

    jobs = scraper.search(search_terms=["welder"], locations=["USA"])

    outcome = scraper.last_outcomes[0]
    assert jobs == []
    assert outcome.jobs_accepted == 0
    assert outcome.stop_reason == BLOCKED
    assert outcome.stop_reason != SOURCE_EXHAUSTED
    assert outcome.error_message == "BLOCKED_BY_CLOUDFLARE"


@pytest.mark.parametrize(
    "error,status,expected",
    [("ROBOTS_DISALLOWED", None, ROBOTS_RESTRICTED),
     ("BLOCKED", 403, BLOCKED),
     ("rate limited", 429, "RATE_LIMITED"),
     (None, 200, SOURCE_EXHAUSTED),
     ("parser exploded", 200, "ERROR")],
)
def test_fetch_failures_map_to_distinct_reasons(error, status, expected):
    assert classify_fetch_failure(error, status) == expected


def test_coverage_rolls_up_per_source():
    report = CoverageReport()
    report.add(SourceOutcome(source="board:simplyhired", query="welder",
                             jobs_returned=10, jobs_accepted=8, duplicates=2,
                             pages=2, stop_reason=SOURCE_EXHAUSTED))
    report.add(SourceOutcome(source="board:simplyhired", query="machinist",
                             jobs_returned=25, jobs_accepted=25, pages=5,
                             stop_reason=BUDGET_REACHED))
    row = report.by_source()["board:simplyhired"]

    assert row["queries"] == 2
    assert row["jobs_accepted"] == 33
    assert row["duplicates"] == 2
    assert row["limited_by_nexbase"] == 1
    assert row["limited_by_source"] == 1
    assert row["stop_reasons"] == {SOURCE_EXHAUSTED: 1, BUDGET_REACHED: 1}


# ===========================================================================
# A USA run stays inside the USA
# ===========================================================================
@pytest.mark.parametrize("site", ["bayt", "naukri", "bdjobs"])
def test_a_non_us_board_cannot_enter_a_usa_run(settings, monkeypatch, site):
    """JobSpy also ships Middle East, India and Bangladesh boards."""
    import nexbase.discovery.jobspy_discovery as mod

    called = []
    monkeypatch.setattr(mod, "_scrape_jobs",
                        lambda **kw: called.append(kw["site_name"]) or None)

    mod.JobSpyDiscovery(settings).search(
        search_terms=["welder"], locations=["USA"], site_names=[site])

    assert called == [], f"{site} must never run"


def test_us_sites_still_run_when_a_non_us_one_is_configured(settings, monkeypatch):
    import nexbase.discovery.jobspy_discovery as mod

    called = []
    monkeypatch.setattr(mod, "_scrape_jobs",
                        lambda **kw: called.append(kw["site_name"][0]) or None)

    mod.JobSpyDiscovery(settings).search(
        search_terms=["welder"], locations=["USA"],
        site_names=["indeed", "naukri"])

    assert called == ["indeed"]


def test_indeed_is_told_the_country(settings):
    assert settings.discovery_country == "usa"


# ===========================================================================
# The pipeline below discovery is unchanged
# ===========================================================================
def test_pipeline_order_is_unchanged():
    src = Path("nexbase/pipeline/runner.py").read_text(encoding="utf-8")
    order = [
        "# --- 1. Discovery",
        "# --- 2. Normalization",
        "# --- 3. Job deduplication",
        "# --- 4. Freshness",
        "# --- 5. Company identification",
        "# --- 6. Qualification",
        "# --- 7. Persist every company",
    ]
    positions = [src.index(marker) for marker in order]
    assert positions == sorted(positions), "pipeline stages are out of order"


def test_runner_calls_no_later_phase_provider_and_sends_nothing():
    """Phase 7 wires ZoomInfo only: Apollo, Apify enrichment, verification and outreach stay out."""
    src = Path("nexbase/pipeline/runner.py").read_text(encoding="utf-8")
    for name in ("ApolloProvider", "EmailVerifier", "ZeroBounce", "outreach"):
        assert name not in src, name


def test_domain_cache_ttls_are_untouched():
    s = Settings(_env_file=None)
    assert s.domain_cache_max_age_days == 180
    assert s.domain_negative_cache_days == 7


def test_size_resolver_stays_off_by_default():
    assert Settings(_env_file=None).size_resolver_enabled is False


# ---------------------------------------------------------------------------
# Helpers for the pagination tests
# ---------------------------------------------------------------------------
def _page(url):
    from nexbase.access.fetcher import FetchedPage

    return FetchedPage(url=url, status=200, html="<html></html>", title="",
                       engine="SCRAPLING", used_fallback=False)


def _rows(page, counts, seen_pages, default=None):
    from nexbase.core.models import RawJob

    seen_pages.append(page)
    count = counts.get(page, default if default is not None else 0)
    return [
        RawJob(source_type="JOB_BOARD", source_priority=2,
               source_site="simplyhired", external_id=f"p{page}-{i}",
               title="Welder", company_name="Acme",
               application_url=f"https://x/{page}/{i}")
        for i in range(count)
    ]


# ===========================================================================
# JobSpy reports failure by logging, not by raising
# ===========================================================================
@pytest.mark.parametrize(
    "messages,expected",
    [
        # Measured live, Columbus OH, "machinist".
        (["Glassdoor response status code 400", "Glassdoor: location not parsed"],
         "ERROR"),
        (['ZipRecruiter response status code 403 with response: '
          '{"error_code":"forbidden aa"}'], BLOCKED),
        (["Indeed response status code 429 rate limit"], "RATE_LIMITED"),
        (["blocked by cloudflare"], BLOCKED),
        (["robots.txt disallowed"], ROBOTS_RESTRICTED),
        ([], SOURCE_EXHAUSTED),
    ],
)
def test_a_sources_own_log_explains_an_empty_result(messages, expected):
    from nexbase.discovery.coverage import classify_source_log

    reason, detail = classify_source_log(messages)
    assert reason == expected
    assert (detail is None) == (not messages)


def test_a_silently_failing_jobspy_site_is_not_reported_as_empty(settings, monkeypatch):
    """The defect: a 400 and "no jobs exist" looked identical.

    JobSpy logs the failure to its own `JobSpy:<Site>` logger - which sets
    propagate=False - and returns an empty frame. Without capturing that logger
    the empty frame is all we see.
    """
    import logging

    import pandas as pd

    import nexbase.discovery.jobspy_discovery as mod

    def failing_scrape(**kwargs):
        logging.getLogger("JobSpy:Glassdoor").error(
            "Glassdoor response status code 400")
        logging.getLogger("JobSpy:Glassdoor").error(
            "Glassdoor: location not parsed")
        return pd.DataFrame([])

    monkeypatch.setattr(mod, "_scrape_jobs", failing_scrape)
    discovery = mod.JobSpyDiscovery(settings)
    jobs = discovery.search(search_terms=["machinist"], locations=["Columbus, OH"],
                            site_names=["glassdoor"])

    outcome = discovery.last_outcomes[0]
    assert jobs == []
    assert outcome.stop_reason != SOURCE_EXHAUSTED
    assert "status code 400" in outcome.error_message
    assert discovery.site_status["glassdoor"]["errors"] == 1


def test_a_genuinely_empty_jobspy_site_still_reads_as_exhausted(settings, monkeypatch):
    """Silence must keep meaning "nothing found", not "something broke"."""
    import pandas as pd

    import nexbase.discovery.jobspy_discovery as mod

    monkeypatch.setattr(mod, "_scrape_jobs", lambda **kw: pd.DataFrame([]))
    discovery = mod.JobSpyDiscovery(settings)
    discovery.search(search_terms=["machinist"], locations=["Columbus, OH"],
                     site_names=["indeed"])

    outcome = discovery.last_outcomes[0]
    assert outcome.stop_reason == SOURCE_EXHAUSTED
    assert outcome.error_message is None
    assert discovery.site_status["indeed"]["errors"] == 0


# ===========================================================================
# ATS slices are downloaded once per run, not once per probe
#
# `ats_scrapers.Client.load(ats=...)` re-downloads every call - only the full
# 16.9 GB snapshot is memoised upstream, and that is the path NexBase never
# takes. A live three-term run fetched ashby, paylocity and bamboohr four
# times each, greenhouse and workable three times.
# ===========================================================================
def _frame(rows=3):
    import pandas as pd

    return pd.DataFrame([
        {"global_id": f"g{i}", "title": "Machinist", "company": f"Acme {i}",
         "location": "Mason, OH", "country_iso": "US",
         "url": f"https://boards.greenhouse.io/a/jobs/{i}"}
        for i in range(rows)
    ])


def test_a_slice_is_loaded_once_however_often_it_is_asked_for():
    from nexbase.discovery.ats_discovery import ATSSliceCache

    cache = ATSSliceCache()
    loads = []

    def loader():
        loads.append("greenhouse")
        return _frame()

    first = cache.get("greenhouse", loader)
    for _ in range(9):
        again = cache.get("greenhouse", loader)
        assert again is first, "every later ask must reuse the same object"

    assert loads == ["greenhouse"]
    assert cache.stats() == {"slices": 1, "downloads": 1, "reused": 9}


def test_different_slices_are_cached_separately():
    from nexbase.discovery.ats_discovery import ATSSliceCache

    cache = ATSSliceCache()
    loads = []

    def loader(name):
        loads.append(name)
        return _frame()

    for name in ("greenhouse", "lever", "greenhouse", "lever", "ashby"):
        cache.get(name, lambda n=name: loader(n))

    assert loads == ["greenhouse", "lever", "ashby"]
    assert cache.stats()["slices"] == 3
    assert cache.stats()["downloads"] == 3


def test_concurrent_asks_for_one_slice_trigger_a_single_download():
    """Two probes wanting `greenhouse` at once must not both download it."""
    import threading

    from nexbase.discovery.ats_discovery import ATSSliceCache

    cache = ATSSliceCache()
    loads = []
    started = threading.Barrier(8)

    def loader():
        loads.append(1)
        # Hold the lock long enough that every waiter is genuinely queued.
        time.sleep(0.05)
        return _frame()

    results = []

    def worker():
        started.wait(timeout=5)
        results.append(cache.get("greenhouse", loader))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(loads) == 1, f"{len(loads)} concurrent downloads of one slice"
    assert len(results) == 8
    assert all(r is results[0] for r in results)


def test_a_failed_download_is_not_cached_as_a_success():
    from nexbase.discovery.ats_discovery import ATSSliceCache

    cache = ATSSliceCache()
    calls = []

    def flaky():
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("connection reset")
        return _frame()

    with pytest.raises(RuntimeError):
        cache.get("greenhouse", flaky)
    assert cache.stats()["downloads"] == 0, "a raised loader must cache nothing"

    frame = cache.get("greenhouse", flaky)
    assert frame is not None
    assert cache.stats()["downloads"] == 1
    assert len(calls) == 2, "the next caller must retry, not inherit the failure"


def test_an_empty_download_is_not_cached_as_a_success():
    """Returning nothing must not lock the slice out for the rest of the run."""
    from nexbase.discovery.ats_discovery import ATSSliceCache

    cache = ATSSliceCache()
    calls = []

    def sometimes_nothing():
        calls.append(1)
        return None if len(calls) == 1 else _frame()

    assert cache.get("lever", sometimes_nothing) is None
    assert cache.stats()["downloads"] == 0
    assert cache.get("lever", sometimes_nothing) is not None
    assert len(calls) == 2


def test_the_cached_client_loads_each_slice_once_across_repeated_searches(monkeypatch):
    """End to end through the real subclass, with the library's load() counted."""
    import ats_scrapers

    from nexbase.discovery.ats_discovery import ATSSliceCache, build_cached_client

    downloads = []

    def fake_load(self, *, ats=None, date=None):
        downloads.append(str(ats))
        return _frame()

    monkeypatch.setattr(ats_scrapers.Client, "load", fake_load)

    cache = ATSSliceCache()
    client = build_cached_client(cache)
    for _ in range(4):
        client.load(ats="greenhouse")
        client.load(ats="lever")

    assert downloads == ["greenhouse", "lever"], downloads
    assert cache.stats()["downloads"] == 2
    assert cache.stats()["reused"] == 6


def test_the_full_snapshot_and_dated_deltas_bypass_the_slice_cache(monkeypatch):
    """Only per-ATS slices are ours to cache; the library memoises the rest."""
    import ats_scrapers

    from nexbase.discovery.ats_discovery import ATSSliceCache, build_cached_client

    seen = []

    def fake_load(self, *, ats=None, date=None):
        seen.append((str(ats), str(date)))
        return _frame()

    monkeypatch.setattr(ats_scrapers.Client, "load", fake_load)

    cache = ATSSliceCache()
    client = build_cached_client(cache)
    client.load()
    client.load()
    client.load(date="2026-09-15")

    assert len(seen) == 3, "nothing but a slice may be served from our cache"
    assert cache.stats()["downloads"] == 0


def test_every_ats_source_in_a_run_shares_one_slice_cache(settings, monkeypatch):
    """Ten ATS sources and many queries must not mean repeat downloads."""
    import nexbase.discovery.registry as reg
    from nexbase.discovery.planner import NATIONWIDE, TitleVariant

    caches = []

    class FakeATS:
        def __init__(self, settings, log, slice_cache=None):
            caches.append(slice_cache)
            self.errors = []

        def search(self, **kwargs):
            return []

    monkeypatch.setattr(reg, "ATSDiscovery", FakeATS)
    query = reg.SourceQuery(TitleVariant("machinist", "INPUT"), NATIONWIDE, "", 336)
    for source in reg.build_registry(settings).sources():
        if source.source_class is reg.SourceClass.ATS:
            source.adapter(query)
            source.adapter(query)

    assert len(caches) == 2 * len(reg.ATS_PORTALS)
    assert len({id(c) for c in caches}) == 1


def test_a_standalone_discovery_still_gets_its_own_cache(settings):
    """No runner, no shared cache - but still never two loads in one call."""
    from nexbase.discovery.ats_discovery import ATSDiscovery, ATSSliceCache

    discovery = ATSDiscovery(settings)
    assert isinstance(discovery.slice_cache, ATSSliceCache)
    assert discovery.slice_cache.stats()["downloads"] == 0
