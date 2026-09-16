"""Cross-source deduplication and the four-tier freshness rules."""
from __future__ import annotations

from datetime import timedelta

import pytest

from nexbase.core.enums import FreshnessPriority
from nexbase.pipeline.dedupe import dedupe_jobs
from nexbase.pipeline.freshness import FreshnessFilter, evaluate_freshness
from nexbase.pipeline.normalize import normalize_job


def _norm(jobs):
    return [normalize_job(j) for j in jobs]


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------
def test_legal_suffix_variants_collapse(make_job):
    jobs = _norm([
        make_job(company="Acme Manufacturing Inc.", title="Plant Manager"),
        make_job(company="Acme Manufacturing LLC", title="Welder"),
        make_job(company="Acme Manufacturing", title="Buyer"),
    ])
    companies = dedupe_jobs(jobs)
    assert len(companies) == 1
    assert companies[0].hiring_intensity == 3


def test_same_company_across_sources_merges_into_one(make_job):
    """Indeed (real domain), ZipRecruiter (none) and an ATS row are one company."""
    jobs = _norm([
        make_job(
            company="Acme Manufacturing Inc.",
            title="Plant Manager",
            source_site="indeed",
            company_url="https://www.indeed.com/cmp/Acme-Manufacturing",
            company_website="https://www.acme-mfg.com",
        ),
        make_job(company="Acme Manufacturing", title="Welder", source_site="zip_recruiter"),
        make_job(
            company="Acme Manufacturing LLC",
            title="Buyer",
            source_type="ATS",
            source_priority=1,
            source_site="greenhouse",
        ),
    ])
    companies = dedupe_jobs(jobs)
    assert len(companies) == 1, "cross-source dedup failed"
    company = companies[0]
    assert company.domain == "acme-mfg.com"
    assert company.hiring_intensity == 3
    assert set(company.source_sites) == {"indeed", "zip_recruiter", "greenhouse"}


def test_genuine_name_collision_stays_separate(make_job):
    jobs = _norm([
        make_job(company="Summit Construction", title="Foreman",
                 company_website="https://summit-a.com"),
        make_job(company="Summit Construction", title="Estimator",
                 company_website="https://summit-b.com"),
    ])
    companies = dedupe_jobs(jobs)
    assert len(companies) == 2
    assert {c.domain for c in companies} == {"summit-a.com", "summit-b.com"}


def test_cross_posted_role_counted_once(make_job):
    """Same title+location on two boards is one opening, not two."""
    jobs = _norm([
        make_job(company="Acme", title="Plant Manager", source_site="indeed",
                 company_website="https://acme.com", external_id="a"),
        make_job(company="Acme", title="Plant Manager", source_site="linkedin",
                 company_website="https://acme.com", external_id="b"),
    ])
    companies = dedupe_jobs(jobs)
    assert len(companies) == 1
    assert companies[0].hiring_intensity == 1
    assert companies[0].jobs[0].raw.get("cross_posted_on") == ["indeed", "linkedin"]


def test_company_facts_surface_from_richest_sighting(make_job):
    jobs = _norm([
        make_job(company="Acme", title="Welder", source_site="zip_recruiter"),
        make_job(company="Acme", title="Buyer", source_site="indeed",
                 company_website="https://acme.com",
                 employees="51 to 200", industry="Manufacturing"),
    ])
    company = dedupe_jobs(jobs)[0]
    assert company.observed_employee_count == "51 to 200"
    assert company.observed_industry == "Manufacturing"
    assert company.company_website == "https://acme.com"


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "days,expected_priority,keep",
    [
        (0.1, FreshnessPriority.P2, True),   # < 5 hours -> demoted, per the brief
        (0.5, FreshnessPriority.P1, True),   # 12 hours
        (3.0, FreshnessPriority.P1, True),
        (7.0, FreshnessPriority.P1, True),
        (9.0, FreshnessPriority.P2, True),
        (13.0, FreshnessPriority.P3, True),  # "approaching 14 days"
        (14.5, None, False),                 # hard exclusion
    ],
)
def test_freshness_tiers(make_job, now, days, expected_priority, keep):
    job = normalize_job(make_job(days_old=days))
    result = evaluate_freshness(job, now=now)
    assert result.keep is keep
    assert result.priority == (int(expected_priority) if expected_priority else None)


def test_five_hour_floor_is_real(make_job, now):
    just_posted = normalize_job(make_job(days_old=2 / 24))     # 2 hours
    six_hours = normalize_job(make_job(days_old=6 / 24))
    assert evaluate_freshness(just_posted, now=now).priority == 2
    assert evaluate_freshness(just_posted, now=now).reason == "BELOW_P1_MIN_AGE"
    assert evaluate_freshness(six_hours, now=now).priority == 1


def test_missing_and_future_dates_are_rejected_not_guessed(make_job, now):
    job = normalize_job(make_job())
    job.posted_at = None
    assert evaluate_freshness(job, now=now).reason == "MISSING_POSTING_DATE"

    future = normalize_job(make_job(days_old=-3))
    assert evaluate_freshness(future, now=now).reason == "FUTURE_POSTING_DATE"


def test_company_dropped_when_all_jobs_stale(make_job, settings, now):
    jobs = _norm([make_job(company="Stale Co", days_old=30)])
    companies = dedupe_jobs(jobs)
    fresh = FreshnessFilter(settings).filter(companies, now=now)
    assert fresh == []
