"""Phase 4: normalization, job deduplication, freshness, company identification."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nexbase.core.models import RawJob
from nexbase.pipeline.company_identity import identify_companies, prepare_companies, source_ids
from nexbase.pipeline.dedupe import dedupe_postings
from nexbase.pipeline.freshness import SOURCE_WINDOW_KEY, evaluate_freshness
from nexbase.pipeline.normalize import (
    normalize_company_name,
    normalize_job,
    normalize_location,
    normalize_posted_at,
    normalize_title,
)


def _norm(jobs):
    return [normalize_job(j) for j in jobs]


def _companies(make_job_list, settings, now):
    return prepare_companies(make_job_list, settings, now)[0]


# ===========================================================================
# Normalization
# ===========================================================================
@pytest.mark.parametrize("location", [
    "Toledo, OH", "Toledo, OH, US", "Toledo, Ohio, United States", "TOLEDO, OH 43604",
])
def test_every_source_spelling_of_a_location_normalizes_the_same(location):
    loc = normalize_location(location)
    assert (loc.city, loc.state, loc.country, loc.key) == ("Toledo", "OH", "US", "toledo, oh")


@pytest.mark.parametrize("location,country,expected", [
    ("Hybrid in Boston, MA", None, ("Boston", "MA", "US", False)),
    ("Remote - US", None, (None, None, "US", True)),
    ("US", None, (None, None, "US", False)),
    ("Berlin", "DE", (None, None, "DE", False)),
    ("Toronto, Canada", None, (None, None, "NON_US", False)),
    ("(Multiple states)", None, (None, None, None, False)),
])
def test_location_parts_are_only_what_the_source_said(location, country, expected):
    loc = normalize_location(location, country)
    assert (loc.city, loc.state, loc.country, loc.remote) == expected


def test_a_date_only_posting_keeps_its_precision():
    assert normalize_posted_at("2026-09-02") == (datetime(2026, 9, 2, tzinfo=timezone.utc), "DAY")
    posted, precision = normalize_posted_at("2026-09-11T18:34:38-05:00")
    assert (posted, precision) == (datetime(2026, 9, 11, 23, 34, 38, tzinfo=timezone.utc), "TIME")
    assert normalize_posted_at(None) == (None, None)


def test_titles_are_normalized_only_in_form():
    assert normalize_title("Plant Manager &amp; Supervisor – Days.") == "plant manager & supervisor - days"
    assert normalize_title("Machinist (1st Shift)") != normalize_title("Machinist (2nd Shift)")


@pytest.mark.parametrize("left,right", [
    ("John's Welding & Towing, Inc.", "Johns Welding and Towing"),
    ("Acmé Manufacturing Co., Inc.", "ACME MANUFACTURING"),
    ("The Home Depot", "Home Depot"),
])
def test_company_name_spellings_fold_together(left, right):
    assert normalize_company_name(left) == normalize_company_name(right)


@pytest.mark.parametrize("overrides,reason", [
    ({"company": None}, "NO_COMPANY_NAME"),
    ({"location": "Toronto, Canada"}, "NOT_US"),
])
def test_out_of_scope_postings_are_screened_with_a_reason(make_job, overrides, reason):
    assert normalize_job(make_job(**overrides)).screen_reason == reason


def test_a_stated_non_full_time_type_is_screened(make_job):
    job = make_job()
    job.employment_type = "Part-time"
    assert normalize_job(job).screen_reason.startswith("NOT_FULL_TIME")


# ===========================================================================
# Job deduplication
# ===========================================================================
def test_the_same_portal_row_twice_is_one_job(make_job):
    dated = make_job(company="Acme", external_id="in-1")
    undated = make_job(company="Acme", external_id="in-1")
    undated.posted_at = None

    kept, duplicates = dedupe_postings(_norm([undated, dated]))

    assert len(kept) == 1 and kept[0].posted_at is not None, "the dated copy is kept"
    assert [d.reason for d in duplicates] == ["SAME_SOURCE_ID"]
    assert kept[0].raw["duplicates"][0]["reason"] == "SAME_SOURCE_ID"


def test_a_cross_post_written_two_ways_collapses_with_an_audit_trail(make_job):
    kept, duplicates = dedupe_postings(_norm([
        make_job(company="Acme Mfg Inc.", title="Welder", location="Toledo, OH",
                 source_site="indeed", external_id="in-9"),
        make_job(company="ACME MFG", title="Welder", location="Toledo, Ohio, United States",
                 source_site="talent_com", external_id="tc-4"),
    ]))
    assert len(kept) == 1 and len(duplicates) == 1
    assert kept[0].raw["cross_posted_on"] == ["indeed", "talent_com"]
    assert kept[0].raw["duplicates"][0]["source_site"] in {"indeed", "talent_com"}


def test_postings_without_a_state_are_never_merged_across_portals(make_job):
    kept, duplicates = dedupe_postings(_norm([
        make_job(company="Acme", title="Welder", location="Remote",
                 source_site="indeed", external_id="a"),
        make_job(company="Acme", title="Welder", location="Remote",
                 source_site="talent_com", external_id="b"),
    ]))
    assert len(kept) == 2 and duplicates == []


def test_same_name_postings_on_different_employer_domains_stay_apart(make_job):
    kept, _ = dedupe_postings(_norm([
        make_job(company="Summit", title="Foreman", company_website="https://summit-a.com",
                 source_site="indeed", external_id="a"),
        make_job(company="Summit", title="Foreman", company_website="https://summit-b.com",
                 source_site="linkedin", external_id="b"),
    ]))
    assert len(kept) == 2


def test_different_titles_are_different_jobs(make_job):
    kept, _ = dedupe_postings(_norm([
        make_job(company="Acme", title="Machinist (1st Shift)", source_site="indeed", external_id="a"),
        make_job(company="Acme", title="Machinist (2nd Shift)", source_site="talent_com", external_id="b"),
    ]))
    assert len(kept) == 2


# ===========================================================================
# Freshness
# ===========================================================================
@pytest.mark.parametrize("days,keep,reason", [
    (0.0, True, None), (13.9, True, None), (14.2, False, "STALE"), (-3, False, "FUTURE_POSTING_DATE"),
])
def test_timestamped_postings_are_aged_in_hours(make_job, now, days, keep, reason):
    result = evaluate_freshness(normalize_job(make_job(days_old=days)), now=now)
    assert (result.keep, result.reason) == (keep, reason)


def test_date_only_postings_are_aged_in_calendar_days(make_job, now):
    job = normalize_job(make_job())
    job.date_precision = "DAY"
    job.posted_at = datetime(2026, 5, 18, tzinfo=timezone.utc)   # 14 calendar days before now
    assert evaluate_freshness(job, now=now).keep is True
    job.posted_at = datetime(2026, 5, 17, tzinfo=timezone.utc)
    assert evaluate_freshness(job, now=now).reason == "STALE"


def test_a_timestamp_a_few_minutes_ahead_is_clock_skew_not_the_future(make_job, now):
    job = normalize_job(make_job())
    job.posted_at = now + timedelta(minutes=20)
    assert evaluate_freshness(job, now=now).keep is True


@pytest.mark.parametrize("window,keep,reason", [
    (None, False, "MISSING_POSTING_DATE"),
    (336, True, "UNDATED_WITHIN_SOURCE_WINDOW"),
    (360, False, "MISSING_POSTING_DATE"),
])
def test_a_missing_date_is_decided_by_the_sources_own_filter(make_job, now, window, keep, reason):
    job = normalize_job(make_job())
    job.posted_at = None
    job.provenance = {SOURCE_WINDOW_KEY: window}
    result = evaluate_freshness(job, now=now)
    assert (result.keep, result.reason, result.age_days) == (keep, reason, None)


def test_a_company_is_built_only_from_fresh_postings(make_job, settings, now):
    companies, _, _, stale = prepare_companies([
        make_job(company="Acme", title="Welder", days_old=2, external_id="f"),
        make_job(company="Acme", title="Buyer", days_old=40, external_id="s"),
        make_job(company="Stale Co", title="Welder", days_old=30, external_id="x"),
    ], settings, now)

    assert [c.company.company_name_normalized for c in companies] == ["acme"]
    assert companies[0].hiring_intensity == 1
    assert sorted(result.reason for _, result in stale) == ["STALE", "STALE"]


# ===========================================================================
# Company identification: domain > employer URL > source ID > name + location
# ===========================================================================
def test_legal_suffix_variants_are_one_company(make_job, settings, now):
    companies = _companies([
        make_job(company="Acme Manufacturing Inc.", title="Plant Manager"),
        make_job(company="Acme Manufacturing LLC", title="Welder"),
        make_job(company="Acme Manufacturing", title="Buyer"),
    ], settings, now)
    assert len(companies) == 1 and companies[0].hiring_intensity == 3


def test_a_website_domain_joins_postings_whatever_the_name(make_job, settings, now):
    companies = _companies([
        make_job(company="Acme", title="Welder", company_website="https://acme-mfg.com",
                 location="Toledo, OH", external_id="a"),
        make_job(company="Acme Manufacturing Group", title="Buyer",
                 company_website="https://www.acme-mfg.com", location="Austin, TX", external_id="b"),
    ], settings, now)
    assert len(companies) == 1
    company = companies[0].company
    assert (company.identity_basis, company.identity_key) == ("DOMAIN", "domain:acme-mfg.com")


def test_an_apply_url_domain_only_joins_postings_that_share_the_name(make_job, settings, now):
    raw = [
        make_job(company="Acme", title="Welder", location="Toledo, OH", external_id="a"),
        make_job(company="Beta", title="Buyer", location="Austin, TX", external_id="b"),
        make_job(company="Acme", title="Driver", location="Dayton, OH", external_id="c"),
    ]
    for job, url in zip(raw, ("https://careers.shared-host.com/a", "https://careers.shared-host.com/b",
                              "https://careers.shared-host.com/c")):
        job.application_url = url
    companies = _companies(raw, settings, now)

    by_name = {c.company.company_name_normalized: c.company for c in companies}
    assert set(by_name) == {"acme", "beta"}, "a shared careers host is not one employer"
    assert by_name["acme"].hiring_intensity == 2
    assert by_name["acme"].identity_basis == "EMPLOYER_URL"


def test_a_source_company_id_joins_postings_across_states(make_job, settings, now):
    indeed = "https://www.indeed.com/cmp/Rumpke-Waste-&-Recycling"
    companies = _companies([
        make_job(company="Rumpke", title="Driver", location="Columbus, OH",
                 company_url=indeed, external_id="a"),
        make_job(company="Rumpke Waste and Recycling", title="Mechanic", location="Louisville, KY",
                 company_url=indeed, external_id="b"),
    ], settings, now)
    assert len(companies) == 1
    company = companies[0].company
    assert company.identity_basis == "SOURCE_ID"
    assert company.identity_key == "source:indeed:rumpke-waste-&-recycling"


@pytest.mark.parametrize("url,ats,raw,expected", [
    ("https://job-boards.greenhouse.io/1uphealth/jobs/47", None, {}, "greenhouse:1uphealth"),
    ("https://jobs.lever.co/100ms/4c9f19c7", None, {}, "lever:100ms"),
    ("https://350.bamboohr.com/careers/63", None, {}, "bamboohr:350"),
    ("https://4leggedkidsinc.applytojob.com/apply/jobs/details/kQf", None, {}, "jazzhr:4leggedkidsinc"),
    ("https://www.linkedin.com/company/chase-doors", None, {}, "linkedin:chase-doors"),
    ("https://recruiting.paylocity.com/Recruiting/Jobs/Details/4498567", "paylocity",
     {"ats_id": "8e0feae7-e42f:4498567"}, "paylocity:8e0feae7-e42f"),
])
def test_source_company_ids_come_from_verified_url_shapes(url, ats, raw, expected):
    job = normalize_job(RawJob("ATS", 1, title="Welder", company_name="X", application_url=url,
                               ats_platform=ats, raw=raw))
    assert expected in source_ids(job)


def test_a_workable_job_link_is_not_a_tenant():
    job = normalize_job(RawJob("ATS", 1, title="Welder", company_name="X",
                               application_url="https://apply.workable.com/j/E8A419651D"))
    assert source_ids(job) == set()


def test_same_name_in_different_states_is_two_companies_with_distinct_keys(make_job, settings, now):
    companies = _companies([
        make_job(company="Summit Construction", title="Carpenter", location="Denver, CO"),
        make_job(company="Summit Construction", title="Electrician", location="Tampa, FL"),
    ], settings, now)
    keys = {c.company.dedup_key for c in companies}
    assert keys == {("NO_DOMAIN", "name:summit construction|co"),
                    ("NO_DOMAIN", "name:summit construction|fl")}


def test_weaker_evidence_never_joins_two_domains(make_job, settings, now):
    companies = _companies([
        make_job(company="Summit Construction", title="Foreman", location="Denver, CO",
                 company_website="https://summit-a.com", external_id="a"),
        make_job(company="Summit Construction", title="Estimator", location="Denver, CO",
                 company_website="https://summit-b.com", external_id="b"),
        make_job(company="Summit Construction", title="Laborer", location="Denver, CO",
                 external_id="c"),
    ], settings, now)
    domains = sorted(c.company.domain for c in companies)
    assert domains == ["", "summit-a.com", "summit-b.com"], "the domainless posting is not guessed"


def test_a_shared_source_id_does_not_join_two_domains(make_job, settings, now):
    profile = "https://www.indeed.com/cmp/Summit"
    companies, stats = identify_companies([
        (job, None) for job in _norm([
            make_job(company="Summit", title="A", company_url=profile,
                     company_website="https://summit-a.com", external_id="a"),
            make_job(company="Summit", title="B", company_url=profile,
                     company_website="https://summit-b.com", external_id="b"),
        ])])
    assert len(companies) == 2 and stats["conflicts"] >= 1


def test_company_facts_surface_from_the_richest_sighting(make_job, settings, now):
    company = _companies([
        make_job(company="Acme", title="Welder", source_site="zip_recruiter", external_id="z"),
        make_job(company="Acme", title="Buyer", source_site="indeed", external_id="i",
                 company_website="https://acme.com", employees="51 to 200",
                 industry="Manufacturing"),
    ], settings, now)[0].company
    assert company.observed_employee_count == "51 to 200"
    assert company.observed_industry == "Manufacturing"
    assert company.company_website == "https://acme.com"


# ===========================================================================
# Through the runner
# ===========================================================================
def test_two_same_name_companies_in_different_states_persist_as_two_rows(
        settings, recording_repo, make_job, now):
    """Their shared (NO_DOMAIN, name) key used to merge them in the database."""
    from nexbase.pipeline.runner import PipelineRunner
    from tests.test_pipeline_e2e import StubAccess

    runner = PipelineRunner(settings=settings, repo=recording_repo, access=StubAccess({}, settings=settings))
    runner.run(raw_jobs=[
        make_job(company="Summit Construction", title="Carpenter", location="Denver, CO", external_id="a"),
        make_job(company="Summit Construction", title="Electrician", location="Tampa, FL", external_id="b"),
    ], now=now, stop_at="before_contacts")

    rows = recording_repo.calls["company"]
    assert len({(r["normalized_domain"], r["normalized_name"]) for r in rows}) == 2
    assert {r["identity_basis"] for r in rows} == {"NAME_LOCATION"}
    assert all(j["state"] in ("CO", "FL") for j in recording_repo.calls["job"])


def test_an_undated_row_the_source_filtered_reaches_a_company(settings, recording_repo, make_job, now):
    from nexbase.pipeline.runner import PipelineRunner
    from tests.test_pipeline_e2e import StubAccess

    job = make_job(company="Eliason Corporation", title="Welder", location="Cincinnati, OH")
    job.posted_at = None
    job.provenance = {SOURCE_WINDOW_KEY: 336, "portal": "linkedin"}
    report = PipelineRunner(settings=settings, repo=recording_repo,
                            access=StubAccess({}, settings=settings)).run(
        raw_jobs=[job], now=now, stop_at="before_contacts")

    assert report.companies == 1 and report.discarded == {}
    persisted = recording_repo.calls["job"][0]
    assert persisted["freshness_reason"] == "UNDATED_WITHIN_SOURCE_WINDOW"
    assert persisted["age_days"] is None
