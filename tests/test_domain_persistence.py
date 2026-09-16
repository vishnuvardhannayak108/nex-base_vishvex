"""Domain persistence and cross-run reuse."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nexbase.pipeline.domain_resolver import (
    Confidence,
    DomainCache,
    DomainResult,
    domain_row,
)
from nexbase.pipeline.runner import PipelineRunner
from tests.conftest import RecordingRepo
from tests.test_pipeline_e2e import StubAccess

NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


def _result(conf, domain="acmemetalfab.com", affinity=0.95):
    """A searched result. `signals` carries the affinity the scorer computed;
    a SEARCH result without one is the zero-affinity case the policy refuses."""
    return DomainResult(domain, conf, "SEARCH",
                        evidence_url=f"https://{domain}",
                        evidence_snippet="Acme Metal Fabrication",
                        signals={"name_affinity": affinity})


# ---------------------------------------------------------------------------
# What gets written
# ---------------------------------------------------------------------------
def test_high_confidence_is_persisted():
    row = domain_row(_result(Confidence.HIGH), now=NOW)
    assert row["domain"] == "acmemetalfab.com"
    assert row["domain_confidence"] == "HIGH"
    assert row["domain_source"] == "SEARCH"
    assert row["domain_evidence_url"] == "https://acmemetalfab.com"
    assert row["domain_resolved_at"].startswith("2026-09-14")


def test_medium_confidence_is_not_attached():
    """Policy: only an unambiguous match becomes the company's domain.

    MEDIUM is a plausible candidate, not a fact, so it is never written as the
    working domain. It is surfaced for review instead.
    """
    assert domain_row(_result(Confidence.MEDIUM), now=NOW) == {}
    assert _result(Confidence.MEDIUM).needs_review is True


def test_a_searched_domain_with_no_name_affinity_is_not_attached():
    """american scaffolding -> amscaf.com: accepted on branding alone before."""
    result = _result(Confidence.HIGH, domain="amscaf.com", affinity=0.0)
    assert result.acceptable is False
    assert result.needs_review is True
    assert domain_row(result, now=NOW) == {}


def test_a_source_supplied_domain_needs_no_affinity_score():
    """EXISTING and SOURCE_URL are observed evidence, not searches."""
    for source in ("EXISTING", "SOURCE_URL"):
        result = DomainResult("acmemetalfab.com", Confidence.HIGH, source)
        assert result.acceptable is True, source
        assert result.needs_review is False, source


def test_low_confidence_is_never_persisted():
    assert domain_row(_result(Confidence.LOW), now=NOW) == {}


def test_result_without_a_domain_is_never_persisted():
    assert domain_row(DomainResult(None, Confidence.HIGH, "SEARCH"), now=NOW) == {}


def test_evidence_metadata_round_trips():
    row = domain_row(_result(Confidence.HIGH), now=NOW)
    assert set(row) == {
        "domain", "domain_confidence", "domain_source",
        "domain_evidence_url", "domain_resolved_at",
    }


# ---------------------------------------------------------------------------
# Cache validity
# ---------------------------------------------------------------------------
def _row(days_old=1, domain="acmemetalfab.com", confidence="HIGH"):
    stamp = (NOW - timedelta(days=days_old)).isoformat()
    return {"domain": domain, "domain_confidence": confidence,
            "domain_source": "SEARCH", "domain_evidence_url": f"https://{domain}",
            "domain_resolved_at": stamp}


@pytest.mark.parametrize("confidence", ["HIGH"])
def test_verified_cached_domain_is_reused(confidence):
    cache = DomainCache(max_age_days=180)
    got = cache.from_row(_row(confidence=confidence), now=NOW)
    assert got is not None
    assert got.domain == "acmemetalfab.com"
    assert got.confidence is Confidence(confidence)
    assert cache.hits == 1


@pytest.mark.parametrize("confidence", ["LOW", "MEDIUM"])
def test_an_unattachable_cached_row_is_not_reused(confidence):
    """Only a HIGH row may be reused as a verified domain."""
    assert DomainCache().from_row(_row(confidence=confidence), now=NOW) is None


def test_cached_row_without_confidence_is_not_reused():
    row = _row()
    row["domain_confidence"] = None
    assert DomainCache().from_row(row, now=NOW) is None


def test_stale_cached_domain_is_not_reused():
    cache = DomainCache(max_age_days=180)
    assert cache.from_row(_row(days_old=181), now=NOW) is None
    assert cache.from_row(_row(days_old=179), now=NOW) is not None


def test_cached_row_with_aggregator_domain_is_rejected():
    """A platform host was never a valid official domain."""
    assert DomainCache().from_row(_row(domain="indeed.com"), now=NOW) is None


def test_missing_or_empty_cache_row_resolves_nothing():
    assert DomainCache().from_row({}, now=NOW) is None
    assert DomainCache(repo=None).lookup("x.com", "x", now=NOW) is None


# ---------------------------------------------------------------------------
# End-to-end: resolve once, reuse thereafter
# ---------------------------------------------------------------------------
class CountingResolver:
    """Resolves a fixed domain and counts how often a search was needed."""

    def __init__(self, confidence=Confidence.HIGH):
        self.calls: list[str] = []
        self.confidence = confidence

    def resolve(self, company_name, existing_domain=None, source_urls=None,
                location=None):
        self.calls.append(company_name)
        return _result(self.confidence, "mysterymfg.com")


class PersistingRepo(RecordingRepo):
    """Keeps the last upserted company row so the next run can read it back."""

    def __init__(self):
        super().__init__()
        self.stored: dict[tuple, dict] = {}

    def upsert_company(self, data, on_conflict=None):
        self._record("company", data)
        self.stored[(data["normalized_domain"], data["normalized_name"])] = dict(data)
        return "company-1"

    def find_company(self, normalized_domain, normalized_name):
        return self.stored.get((normalized_domain, normalized_name))


def _sizeless(make_job, name="Mystery Mfg"):
    return [make_job(company=name, title=t, days_old=1, employees=None,
                     industry="Industrial Manufacturing", company_website=None,
                     description="Rapidly growing, new facility.",
                     external_id=f"{name}-{t}")
            for t in ("Machinist", "Welder", "Press Operator")]


def _run(settings, repo, resolver, make_job, now):
    runner = PipelineRunner(settings=settings, repo=repo,
                            access=StubAccess({}, settings=settings),
                            domain_resolver=resolver)
    return runner.run(raw_jobs=_sizeless(make_job), now=now)


def test_domain_is_resolved_then_persisted(settings, make_job, now):
    repo, resolver = PersistingRepo(), CountingResolver()
    _run(settings, repo, resolver, make_job, now)

    assert resolver.calls, "a company without a domain must trigger resolution"
    stored = repo.calls["company"][-1]
    assert stored["domain"] == "mysterymfg.com"
    assert stored["domain_confidence"] == "HIGH"
    assert stored["domain_source"] == "SEARCH"
    assert stored["domain_evidence_url"]
    assert stored["domain_resolved_at"]
    # The dedup key is never rewritten by resolution.
    assert stored["normalized_domain"] == "NO_DOMAIN"


def test_second_run_reuses_the_domain_without_searching(settings, make_job, now):
    """The regression this change exists for."""
    repo, resolver = PersistingRepo(), CountingResolver()

    _run(settings, repo, resolver, make_job, now)
    first = len(resolver.calls)
    assert first == 1

    _run(settings, repo, resolver, make_job, now)
    assert len(resolver.calls) == first, "cached domain must skip the search"
    assert repo.calls["company"][-1]["domain"] == "mysterymfg.com"


def test_stale_cached_domain_triggers_a_fresh_search(settings, make_job, now):
    repo, resolver = PersistingRepo(), CountingResolver()
    _run(settings, repo, resolver, make_job, now)

    # Age the stored row past the window.
    for key, row in repo.stored.items():
        row["domain_resolved_at"] = (now - timedelta(days=400)).isoformat()

    _run(settings, repo, resolver, make_job, now)
    assert len(resolver.calls) == 2, "a stale domain must be re-resolved"


def test_low_confidence_domain_is_never_persisted(settings, make_job, now):
    """The LOW verdict is recorded for the cooldown; the domain never is."""
    repo = PersistingRepo()
    _run(settings, repo, CountingResolver(Confidence.LOW), make_job, now)

    stored = repo.calls["company"][-1]
    assert stored.get("domain") in (None, "")
    assert stored.get("domain_evidence_url") is None
    assert stored["domain_confidence"] == "LOW"


def test_official_domain_is_stored_without_any_subdomain_field(settings, make_job, now):
    """Subdomains stay out of persistence entirely for now."""
    repo = PersistingRepo()
    _run(settings, repo, CountingResolver(), make_job, now)

    stored = repo.calls["company"][-1]
    assert stored["domain"] == "mysterymfg.com"
    assert not any("subdomain" in k for k in stored)


def test_size_resolver_is_off_by_default(settings):
    assert settings.size_resolver_enabled is False
    assert settings.domain_resolver_enabled is True


def test_unknown_size_still_needs_review_with_resolver_off(settings, make_job, now):
    repo = PersistingRepo()
    report = _run(settings, repo, CountingResolver(), make_job, now)
    assert report.size_resolution["resolved"] == 0
    assert "EMPLOYEE_SIZE_UNKNOWN" in report.needs_review[0].review_flags
    assert (settings.size_filter_min, settings.size_filter_max) == (11, 200)


# ---------------------------------------------------------------------------
# Negative cache: a failed attempt cools down, it never becomes permanent.
# ---------------------------------------------------------------------------
def _neg_row(days_old=1):
    return {"domain": None, "domain_confidence": "LOW", "domain_source": "SEARCH",
            "domain_resolved_at": (NOW - timedelta(days=days_old)).isoformat()}


def test_recent_failure_suppresses_another_search():
    cache = DomainCache(negative_max_age_days=7)
    assert cache.is_negative_cached(_neg_row(1), now=NOW) is True
    assert cache.negative_hits == 1


def test_expired_failure_allows_a_fresh_search():
    cache = DomainCache(negative_max_age_days=7)
    assert cache.is_negative_cached(_neg_row(8), now=NOW) is False
    assert cache.is_negative_cached(_neg_row(6), now=NOW) is True


def test_negative_cache_ttl_is_configurable():
    assert DomainCache(negative_max_age_days=30).is_negative_cached(
        _neg_row(20), now=NOW) is True
    assert DomainCache(negative_max_age_days=1).is_negative_cached(
        _neg_row(20), now=NOW) is False


def test_company_never_attempted_is_not_negative_cached():
    """No domain is not the same as a failed attempt."""
    cache = DomainCache(negative_max_age_days=7)
    assert cache.is_negative_cached({}, now=NOW) is False
    assert cache.is_negative_cached({"domain": None}, now=NOW) is False
    assert cache.is_negative_cached(
        {"domain_confidence": None, "domain_resolved_at": NOW.isoformat()},
        now=NOW) is False
    assert cache.negative_hits == 0


def test_negative_row_is_never_mistaken_for_a_verified_domain():
    cache = DomainCache(max_age_days=180, negative_max_age_days=7)
    assert cache.from_row(_neg_row(1), now=NOW) is None
    assert cache.hits == 0


def test_verified_row_is_not_negative_cached():
    """HIGH caching must be untouched by the cooldown."""
    cache = DomainCache(max_age_days=180, negative_max_age_days=7)
    for confidence in ("HIGH",):
        row = _row(days_old=1, confidence=confidence)
        assert cache.is_negative_cached(row, now=NOW) is False
        assert cache.from_row(row, now=NOW) is not None


def test_low_then_cooldown_then_reresolution(settings, make_job, now):
    """LOW -> suppressed next run -> re-resolved once the TTL expires."""
    repo, resolver = PersistingRepo(), CountingResolver(Confidence.LOW)

    _run(settings, repo, resolver, make_job, now)
    assert len(resolver.calls) == 1
    assert repo.calls["company"][-1]["domain_confidence"] == "LOW"

    # Second run, still inside the 7-day window: no new search.
    _run(settings, repo, resolver, make_job, now)
    assert len(resolver.calls) == 1, "negative cache must suppress the search"

    # Age the failure past the TTL.
    for row in repo.stored.values():
        row["domain_resolved_at"] = (now - timedelta(days=8)).isoformat()

    _run(settings, repo, resolver, make_job, now)
    assert len(resolver.calls) == 2, "expired failure must be re-resolved"


def test_high_confidence_cache_is_unaffected_by_the_cooldown(settings, make_job, now):
    repo, resolver = PersistingRepo(), CountingResolver(Confidence.HIGH)
    _run(settings, repo, resolver, make_job, now)
    _run(settings, repo, resolver, make_job, now)
    assert len(resolver.calls) == 1
    assert repo.calls["company"][-1]["domain"] == "mysterymfg.com"


def test_negative_cache_default_is_seven_days(settings):
    assert settings.domain_negative_cache_days == 7
    assert settings.domain_cache_max_age_days == 180
