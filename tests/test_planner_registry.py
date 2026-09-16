"""Phase 2: the USA-wide planner and the source registry."""
from __future__ import annotations

import inspect

import pytest

from nexbase.core.errors import DiscoveryError
from nexbase.core.models import RawJob
from nexbase.discovery.coverage import BLOCKED, BUDGET_REACHED, ERROR, SOURCE_EXHAUSTED, SourceOutcome
from nexbase.discovery.planner import (
    COUNTRY,
    NATIONWIDE,
    STATE,
    US_STATES,
    DiscoveryPlanner,
    TitleVariant,
)
from nexbase.discovery.registry import (
    ATS_PORTALS,
    JOBSPY_PORTALS,
    Source,
    SourceClass,
    SourceQuery,
    SourceRegistry,
    build_registry,
)


def _plan(settings, sector="Manufacturing", job="Welder"):
    return DiscoveryPlanner(settings).plan(sector, job)


# ===========================================================================
# Planner: Sector + Job in
# ===========================================================================
def test_a_plan_needs_a_known_sector_and_a_job(settings):
    with pytest.raises(DiscoveryError, match="unknown sector"):
        DiscoveryPlanner(settings).plan("Underwater Basket Weaving", "Welder")
    for job in ("", "   ", None):
        with pytest.raises(DiscoveryError, match="job"):
            DiscoveryPlanner(settings).plan("Manufacturing", job)


def test_sector_and_job_are_the_only_inputs():
    params = list(inspect.signature(DiscoveryPlanner.plan).parameters)
    assert params == ["self", "sector", "job"]


def test_the_users_job_is_always_searched_first(settings):
    plan = _plan(settings, job="  Warehouse   Manager ")
    assert plan.job == "Warehouse Manager"
    assert plan.titles[0] == TitleVariant("Warehouse Manager", "INPUT")


def test_title_variants_come_from_the_same_occupation(settings):
    welder = _plan(settings, job="Welder")
    derived = welder.titles[1:]
    assert derived, "a known job must widen"
    assert welder.matched_soc_codes == ["51-4121"]
    assert all(t.origin == "TAXONOMY" and t.soc_code == "51-4121" for t in derived)
    assert all("welder" in t.title.lower() for t in derived)
    assert "Solderer" not in {t.title for t in derived}, "same SOC, different job"

    warehouse = _plan(settings, "Warehousing & Distribution", "Warehouse Manager")
    assert "Warehouse Supervisor" in {t.title for t in warehouse.titles}


@pytest.mark.parametrize("job", ["Supervisor", "Zebra Wrangler"])
def test_vague_or_unknown_jobs_are_never_widened(settings, job):
    plan = _plan(settings, job=job)
    assert plan.titles == [TitleVariant(job, "INPUT")]
    assert plan.matched_soc_codes == []


def test_variant_count_is_bounded_by_settings(settings):
    assert len(_plan(settings, job="Electrician").titles) == settings.planner_max_title_variants
    settings.planner_max_title_variants = 2
    assert len(_plan(settings, job="Electrician").titles) == 2


def test_geography_is_the_whole_usa(settings):
    plan = _plan(settings)
    assert plan.geo_root == NATIONWIDE and NATIONWIDE.level == COUNTRY
    codes = [g.code for g in plan.geo_partitions]
    assert len(codes) == len(set(codes)) == 51, "50 states + DC"
    assert "DC" in codes and "PR" not in codes
    assert all(g.level == STATE for g in US_STATES)


def test_planning_is_deterministic_apart_from_the_run_key(settings):
    a, b = _plan(settings).to_dict(), _plan(settings).to_dict()
    assert a.pop("run_key") != b.pop("run_key")
    assert a == b
    assert a["hours_old"] == settings.freshness_max_days * 24


# ===========================================================================
# Registry: the catalog
# ===========================================================================
def test_catalog_has_one_handler_per_portal_in_three_classes(settings):
    from nexbase.discovery.board_scrapers import BOARD_SCRAPERS
    from nexbase.discovery.jobspy_discovery import NON_US_SITES, SUPPORTED_SITES

    registry = build_registry(settings)
    portals = [s.portal for s in registry.sources()]
    assert len(portals) == len(set(portals))
    assert {c.value for c in SourceClass} == {"DIRECT", "ATS", "APIFY"}

    for portal in JOBSPY_PORTALS:
        source = registry.get(portal)
        assert (source.source_class, source.handler) == (SourceClass.DIRECT, "jobspy")
    assert set(JOBSPY_PORTALS) <= SUPPORTED_SITES - NON_US_SITES
    assert all(registry.get(p).source_class is SourceClass.DIRECT for p in BOARD_SCRAPERS)
    assert all(registry.get(p).source_class is SourceClass.ATS for p in ATS_PORTALS)


def test_no_portal_is_implemented_by_two_adapter_modules():
    from nexbase.discovery.board_scrapers import BOARD_SCRAPERS
    from nexbase.discovery.jobspy_discovery import SUPPORTED_SITES

    assert not set(BOARD_SCRAPERS) & SUPPORTED_SITES
    assert not set(ATS_PORTALS) & (SUPPORTED_SITES | set(BOARD_SCRAPERS))


def test_a_second_handler_for_a_portal_is_refused():
    registry = SourceRegistry()
    registry.register(_source("indeed", lambda q: ([], SourceOutcome(source="x"))))
    with pytest.raises(DiscoveryError, match="exactly one"):
        registry.register(Source("indeed", SourceClass.APIFY, "apify-actor",
                                 lambda q: ([], SourceOutcome(source="x"))))


def test_sources_are_switched_off_by_settings(settings):
    assert build_registry(settings).get("monster").enabled is False
    settings.sources_disabled = "glassdoor, GREENHOUSE"
    registry = build_registry(settings)
    assert registry.get("glassdoor").enabled is False
    assert registry.get("greenhouse").enabled is False
    assert registry.get("monster").enabled is True
    assert registry.get("indeed").enabled is True


# ===========================================================================
# Registry: running a plan
# ===========================================================================
def _source(portal, adapter, source_class=SourceClass.DIRECT, **kwargs):
    return Source(portal, source_class, "fake", adapter, **kwargs)


def _job(key, site="indeed"):
    return RawJob(source_type="JOB_BOARD", source_priority=2, source_site=site,
                  external_id=key, title="Welder", company_name="Acme")


class Recorder:
    """An adapter that records its queries and answers from a script."""

    def __init__(self, reply=None):
        self.queries: list[SourceQuery] = []
        self.reply = reply or (lambda q: ([], SOURCE_EXHAUSTED))

    def __call__(self, query):
        self.queries.append(query)
        jobs, reason = self.reply(query)
        return jobs, SourceOutcome(source="fake", stop_reason=reason,
                                   jobs_returned=len(jobs))


def test_every_title_is_searched_nationwide_with_provenance(settings):
    plan = _plan(settings)
    adapter = Recorder(lambda q: ([_job(q.title.title)], SOURCE_EXHAUSTED))
    registry = SourceRegistry()
    registry.register(_source("indeed", adapter))

    result = registry.run(plan)

    assert [q.title for q in adapter.queries] == plan.titles
    assert all(q.geo == NATIONWIDE and q.sector == "Manufacturing" for q in adapter.queries)
    assert len(result.jobs) == len(plan.titles)
    provenance = result.jobs[0].provenance
    assert provenance["run_key"] == plan.run_key
    assert (provenance["source_id"], provenance["source_class"], provenance["portal"],
            provenance["handler"]) == ("direct:indeed", "DIRECT", "indeed", "fake")
    assert (provenance["query_title"], provenance["title_origin"]) == ("Welder", "INPUT")
    assert (provenance["geo_code"], provenance["geo_level"]) == ("US", COUNTRY)
    assert provenance["fetched_at"]


def test_a_capped_nationwide_query_fans_out_to_every_state(settings):
    settings.planner_max_title_variants = 2
    plan = _plan(settings)
    first = plan.titles[0]
    adapter = Recorder(lambda q: (
        [], BUDGET_REACHED if q.title == first and q.geo == NATIONWIDE else SOURCE_EXHAUSTED))
    registry = SourceRegistry()
    registry.register(_source("indeed", adapter, max_calls_per_run=500))

    result = registry.run(plan)

    states = [q for q in adapter.queries if q.geo.level == STATE]
    assert len(states) == 51 and {q.title for q in states} == {first}
    assert result.source_status["direct:indeed"]["fanouts"] == 1
    # Breadth first: every title nationwide before any state.
    assert [q.geo for q in adapter.queries[:2]] == [NATIONWIDE, NATIONWIDE]


@pytest.mark.parametrize("reason", [ERROR, BLOCKED])
def test_a_failing_query_never_fans_out(settings, reason):
    adapter = Recorder(lambda q: ([], reason))
    registry = SourceRegistry()
    registry.register(_source("indeed", adapter))
    result = registry.run(_plan(settings))
    assert all(q.geo == NATIONWIDE for q in adapter.queries)
    assert result.source_status["direct:indeed"]["outcome"] == "ERROR"


def test_the_call_budget_is_enforced_and_reported(settings):
    adapter = Recorder(lambda q: ([], BUDGET_REACHED))
    registry = SourceRegistry()
    registry.register(_source("indeed", adapter, max_calls_per_run=3))

    result = registry.run(_plan(settings))

    status = result.source_status["direct:indeed"]
    assert len(adapter.queries) == status["calls"] == 3
    assert status["skipped_queries"] > 0
    last = result.coverage.outcomes[-1]
    assert last.stop_reason == BUDGET_REACHED and last.limited_by_nexbase


def test_calls_to_one_source_are_spaced_by_its_rate_limit(settings, monkeypatch):
    import nexbase.access.ratelimit as ratelimit

    sleeps = []
    monkeypatch.setattr(ratelimit.time, "sleep", sleeps.append)
    registry = SourceRegistry()
    registry.register(_source("indeed", Recorder(), min_interval_seconds=30))
    registry.register(_source("greenhouse", Recorder(), SourceClass.ATS))

    plan = _plan(settings)
    registry.run(plan)

    assert len(sleeps) == len(plan.titles) - 1, "the first call never waits"
    assert all(0 < s <= 30 for s in sleeps)


def test_one_broken_source_is_isolated_and_its_error_tracked(settings):
    def explode(query):
        raise RuntimeError("upstream exploded")

    healthy = Recorder(lambda q: ([_job(q.title.title, "lever")], SOURCE_EXHAUSTED))
    registry = SourceRegistry()
    registry.register(_source("indeed", explode))
    registry.register(_source("lever", healthy, SourceClass.ATS))

    result = registry.run(_plan(settings))

    broken = result.source_status["direct:indeed"]
    assert broken["outcome"] == "ERROR"
    assert broken["errors"] == broken["calls"] > 0
    assert broken["last_error"] == "upstream exploded"
    assert result.source_status["ats:lever"]["outcome"] == "SUCCESS"
    assert result.jobs and all(j.source_site == "lever" for j in result.jobs)


def test_a_repeat_sighting_within_a_portal_is_counted_not_re_emitted(settings):
    registry = SourceRegistry()
    registry.register(_source("indeed", Recorder(lambda q: ([_job("same")], SOURCE_EXHAUSTED))))
    result = registry.run(_plan(settings))
    status = result.source_status["direct:indeed"]
    assert len(result.jobs) == status["jobs_accepted"] == 1
    assert status["duplicates"] == status["calls"] - 1


def test_a_disabled_source_is_reported_and_never_called(settings):
    adapter = Recorder()
    registry = SourceRegistry()
    registry.register(_source("glassdoor", adapter, enabled=False))
    result = registry.run(_plan(settings))
    assert adapter.queries == []
    assert result.source_status["direct:glassdoor"]["outcome"] == "DISABLED"


def test_every_call_is_recorded_in_discovery_runs(settings):
    from tests.conftest import RecordingRepo

    class Repo(RecordingRepo):
        def insert_discovery_run(self, data):
            self._record("run", data)
            return f"run-{len(self.calls['run'])}"

        def update_discovery_run(self, run_id, data):
            self._record("run_update", (run_id, data))

    repo = Repo()
    plan = _plan(settings)
    registry = SourceRegistry()
    registry.register(_source("indeed", Recorder(lambda q: ([_job(q.title.title)], SOURCE_EXHAUSTED))))
    registry.run(plan, repo=repo)

    assert len(repo.calls["run"]) == len(repo.calls["run_update"]) == len(plan.titles)
    opened = repo.calls["run"][0]
    assert (opened["run_key"], opened["source"], opened["search_term"]) == (
        plan.run_key, "direct:indeed", "Welder")
    run_id, closed = repo.calls["run_update"][0]
    assert run_id == "run-1" and closed["status"] == "COMPLETE" and closed["jobs_found"] == 1


def test_an_apify_source_runs_like_any_other(settings):
    registry = SourceRegistry()
    registry.register(_source(
        "craigslist", Recorder(lambda q: ([_job("a1", "craigslist")], SOURCE_EXHAUSTED)),
        SourceClass.APIFY))
    result = registry.run(_plan(settings, job="Zebra Wrangler"))
    assert result.jobs[0].provenance["source_class"] == "APIFY"
    assert result.source_status["apify:craigslist"]["outcome"] == "SUCCESS"


# ===========================================================================
# Real handlers behind the registry
# ===========================================================================
def test_jobspy_is_the_direct_handler_for_its_portals(settings, monkeypatch):
    import pandas as pd

    import nexbase.discovery.jobspy_discovery as jobspy

    calls = []

    def scrape(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame([{"site": "indeed", "id": "in-1", "title": "Welder",
                              "company": "Acme", "job_url": "https://indeed.com/j/1"}])

    monkeypatch.setattr(jobspy, "_scrape_jobs", scrape)
    texas = next(g for g in US_STATES if g.code == "TX")
    query = SourceQuery(TitleVariant("Welder", "INPUT"), texas, "Manufacturing", 336)

    jobs, outcome = build_registry(settings).get("indeed").adapter(query)

    assert calls[0]["site_name"] == ["indeed"]
    assert (calls[0]["search_term"], calls[0]["location"], calls[0]["hours_old"]) == (
        "Welder", "Texas", 336)
    assert [j.external_id for j in jobs] == ["in-1"]
    assert jobs[0].search_industry == "Manufacturing"
    assert outcome.stop_reason == SOURCE_EXHAUSTED


def test_a_failed_ats_slice_is_an_error_not_an_empty_result(settings, monkeypatch):
    import nexbase.discovery.registry as reg

    class FailingATS:
        def __init__(self, *a, **k):
            self.errors = []

        def search(self, **kwargs):
            self.errors.append("greenhouse: download failed")
            return []

    monkeypatch.setattr(reg, "ATSDiscovery", FailingATS)
    query = SourceQuery(TitleVariant("Welder", "INPUT"), NATIONWIDE, "Manufacturing", 336)
    jobs, outcome = build_registry(settings).get("greenhouse").adapter(query)
    assert jobs == []
    assert outcome.stop_reason == ERROR
    assert "download failed" in outcome.error_message


# ===========================================================================
# Pipeline and API
# ===========================================================================
def test_job_provenance_is_persisted_with_the_job(settings, recording_repo, make_job, now):
    from nexbase.pipeline.runner import PipelineRunner
    from tests.test_pipeline_e2e import StubAccess

    registry = SourceRegistry()
    registry.register(_source("indeed", Recorder(lambda q: (
        [make_job(external_id=f"j-{q.title.title}")], SOURCE_EXHAUSTED))))
    plan = _plan(settings)
    runner = PipelineRunner(settings=settings, repo=recording_repo,
                            access=StubAccess({}, settings=settings), registry=registry)

    report = runner.run(plan=plan, now=now, stop_at="before_contacts")

    assert report.raw_jobs == len(plan.titles)
    persisted = recording_repo.calls["job"]
    assert persisted and all(row["provenance"]["run_key"] == plan.run_key for row in persisted)
    assert report.plan["job"] == "Welder"


def test_a_run_without_a_plan_discovers_nothing(settings):
    from nexbase.db.repository import InertRepository
    from nexbase.pipeline.runner import PipelineRunner
    from tests.test_pipeline_e2e import StubAccess

    adapter = Recorder()
    registry = SourceRegistry()
    registry.register(_source("indeed", adapter))
    report = PipelineRunner(settings=settings, repo=InertRepository(),
                            access=StubAccess({}, settings=settings),
                            registry=registry).run(persist=False)
    assert report.raw_jobs == 0 and adapter.queries == []


def test_the_api_run_request_is_sector_and_job_only():
    from pydantic import ValidationError

    from nexbase.api.main import RunRequest

    business = set(RunRequest.model_fields) - {
        "stop_at", "persist", "enrich_linkedin_signal", "include_records"}
    assert business == {"sector", "job"}
    with pytest.raises(ValidationError):
        RunRequest(sector="Nope", job="Welder")
    with pytest.raises(ValidationError):
        RunRequest(sector="Manufacturing", job="")
