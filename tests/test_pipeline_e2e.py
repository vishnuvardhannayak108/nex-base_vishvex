"""End-to-end pipeline runs.

Demonstrates:
  1. raw prospects -> qualified -> rejected with reasons
  2. the employee-size rule (<11 and >200 rejected)
  3. the 14-day job rule
  4. direct-employer filtering
  5. duplicate prevention
  6. POC ranking
  7. no paid provider is called and nothing is sent

Each is asserted below. No network or database is touched.
"""
from __future__ import annotations

import pytest

from nexbase.access.fetcher import AccessLayer, FetchedPage
from nexbase.pipeline.runner import PipelineRunner

TEAM_PAGE = """
<html><head><title>Leadership - Acme</title></head><body>
<div class="vcard"><span class="fn">Jane Whitfield</span>
  <span class="job-title">Chief Executive Officer</span>
  <a class="u-email" href="mailto:jane@acme-mfg.com">email</a></div>
<div class="vcard"><span class="fn">Marcus Reed</span>
  <span class="job-title">Director of Operations</span></div>
<div class="vcard"><span class="fn">Priya Raman</span>
  <span class="job-title">HR Manager</span></div>
</body></html>
"""


class StubAccess(AccessLayer):
    """Access layer that serves canned pages instead of hitting the network."""

    def __init__(self, pages: dict[str, str], settings=None):
        super().__init__(settings=settings)
        self.pages = pages
        self.requested: list[str] = []

    def fetch(self, url: str) -> FetchedPage:
        self.requested.append(url)
        # Longest matching fragment wins, so "acme.com/about" beats "acme.com"
        # and a subdomain never collides with its apex.
        best = max(
            (f for f in self.pages if f in url), key=len, default=None
        )
        if best is not None:
            return FetchedPage(url, 200, self.pages[best], "t", "SCRAPLING", False)
        return FetchedPage(url, 404, "", "", "SCRAPLING", False, blocked=True)


@pytest.fixture
def universe(make_job):
    """A realistic raw-prospect universe covering every acceptance criterion."""
    return [
        # Qualified: right size, right industry, three fresh roles, growth signal.
        make_job(company="Acme Manufacturing Inc.", title="Plant Manager", days_old=1,
                 employees="51 to 200", industry="Manufacturing",
                 company_website="https://www.acme-mfg.com",
                 company_url="https://www.indeed.com/cmp/Acme-Manufacturing",
                 description="Rapidly growing; new facility opening this year.",
                 applicant_count=8, external_id="acme-1"),
        make_job(company="Acme Manufacturing LLC", title="Welder", days_old=2,
                 employees="51 to 200", industry="Manufacturing",
                 company_website="https://www.acme-mfg.com", external_id="acme-2"),
        # Same employer, different board, no domain of its own -> must merge.
        make_job(company="Acme Manufacturing", title="Machinist", days_old=3,
                 source_site="zip_recruiter", external_id="acme-3"),
        # Rejected: intermediary.
        make_job(company="Bolt Staffing Group", title="Recruiter", days_old=1,
                 company_website="https://boltstaffing.com", external_id="bolt-1"),
        # Rejected: stale.
        make_job(company="Cog Industrial", title="Welder", days_old=30,
                 company_website="https://cogindustrial.com", external_id="cog-1"),
        # Rejected: too small.
        make_job(company="Tiny Shop", title="Machinist", days_old=2, employees="1 to 9",
                 industry="Manufacturing", company_website="https://tinyshop.com",
                 external_id="tiny-1"),
        # Rejected: too large.
        make_job(company="Mega Industrial", title="Plant Manager", days_old=1,
                 employees="5,000 to 10,000", industry="Manufacturing",
                 company_website="https://megaindustrial.com", external_id="mega-1"),
    ]


@pytest.fixture
def runner(settings, recording_repo):
    access = StubAccess({"acme-mfg.com": TEAM_PAGE}, settings=settings)
    return PipelineRunner(settings=settings, repo=recording_repo, access=access)


# ---------------------------------------------------------------------------
# 1. raw -> qualified -> rejected, with reasons
# ---------------------------------------------------------------------------
def test_acceptance_1_raw_to_qualified_to_rejected_with_reasons(runner, universe, now):
    report = runner.run(raw_jobs=universe, now=now)

    assert report.raw_jobs == 7
    # Cog's only posting is 30 days old, so it never becomes a company.
    # 6 fresh postings -> Acme (3 sightings) + Bolt + Tiny + Mega = 4 companies.
    assert report.fresh_jobs == 6
    assert report.companies == 4, "Acme's three sightings must be one company"
    assert report.discarded == {"FRESHNESS:STALE": 1}

    qualified = {lead.company_name for lead in report.qualified}
    assert "acme manufacturing" in qualified

    rejected = {r.company_name: r for r in report.rejected}
    assert rejected["bolt staffing group"].reasons == ["STAFFING_AGENCY"]

    rows = {(r["reason"], r["outcome"]) for r in runner.repo.calls["rejection"]
            if r["stage"] == "QUALIFICATION"}
    assert ("STAFFING_AGENCY", "REJECTED") in rows
    # Bolt publishes no industry: a review reason is recorded next to the rejection.
    assert ("INDUSTRY_UNKNOWN", "NEEDS_REVIEW") in rows
    assert "EMPLOYEE_SIZE_BELOW_MIN" in rejected["tiny shop"].reasons
    assert "EMPLOYEE_SIZE_ABOVE_MAX" in rejected["mega industrial"].reasons

    # Every rejection carries a stage and a reason - nothing is unexplained.
    assert all(r.stage and r.reasons for r in report.rejected)


# ---------------------------------------------------------------------------
# 2. employee-size rule
# ---------------------------------------------------------------------------
def test_acceptance_2_employee_size_rule(runner, universe, now):
    report = runner.run(raw_jobs=universe, now=now)

    acme = next(l for l in report.qualified if l.company_name == "acme manufacturing")
    assert acme.employee_size == "51-200"
    assert acme.employee_size_source == "JOB_BOARD"

    assert any("EMPLOYEE_SIZE_BELOW_MIN" in r.reasons for r in report.rejected)
    assert any("EMPLOYEE_SIZE_ABOVE_MAX" in r.reasons for r in report.rejected)
    assert not report.needs_review, "out-of-band size is a rejection, not a review"


# ---------------------------------------------------------------------------
# 3. 14-day job rule
# ---------------------------------------------------------------------------
def test_acceptance_3_fourteen_day_rule(runner, universe, now, recording_repo):
    runner.run(raw_jobs=universe, now=now)

    jobs = recording_repo.calls["job"]
    assert jobs and all(j["is_fresh"] and j["age_days"] <= 14 for j in jobs)
    assert not any(j["title"] == "Welder" and j["age_days"] > 14 for j in jobs)

    stale = [r for r in recording_repo.calls["rejection"] if r["stage"] == "FRESHNESS"]
    assert [r["reason"] for r in stale] == ["STALE"], "the 30-day posting is recorded, not kept"
    assert stale[0]["details"]["company"] == "Cog Industrial"
    assert stale[0]["details"]["age_days"] > 14


# ---------------------------------------------------------------------------
# 4. direct-employer filtering
# ---------------------------------------------------------------------------
def test_acceptance_4_direct_employer_filtering(runner, universe, now, recording_repo):
    runner.run(raw_jobs=universe, now=now)

    companies = {c["normalized_name"]: c for c in recording_repo.calls["company"]}
    assert companies["bolt staffing group"]["is_staffing_agency"] is True
    assert companies["bolt staffing group"]["is_direct_employer"] is False
    assert companies["acme manufacturing"]["is_direct_employer"] is True


# ---------------------------------------------------------------------------
# 5. duplicate prevention
# ---------------------------------------------------------------------------
def test_acceptance_5_duplicate_prevention(runner, universe, now, recording_repo):
    report = runner.run(raw_jobs=universe, now=now)

    acme_rows = [
        c for c in recording_repo.calls["company"]
        if c["normalized_name"] == "acme manufacturing"
    ]
    assert len(acme_rows) == 1, "one company row despite three source sightings"
    assert acme_rows[0]["normalized_domain"] == "acme-mfg.com"
    assert acme_rows[0]["hiring_intensity"] == 3

    acme = next(l for l in report.qualified if l.company_name == "acme manufacturing")
    assert set(acme.source_sites) == {"indeed", "zip_recruiter"}


def test_duplicate_prevention_is_idempotent_across_runs(runner, universe, now, recording_repo):
    runner.run(raw_jobs=universe, now=now)
    first = len(recording_repo.calls["company"])
    runner.run(raw_jobs=universe, now=now)
    # Same upsert keys on the second pass; no new identities invented.
    keys = {(c["normalized_domain"], c["normalized_name"]) for c in recording_repo.calls["company"]}
    assert len(keys) == first


# ---------------------------------------------------------------------------
# 6. POC ranking
# ---------------------------------------------------------------------------
def test_acceptance_6_poc_ranking(runner, universe, now):
    report = runner.run(raw_jobs=universe, now=now)

    acme = next(l for l in report.qualified if l.company_name == "acme manufacturing")
    assert acme.contact_discovery == "COMPLETED"
    assert [(c["name"], c["title_priority"]) for c in acme.contacts] == [
        ("Jane Whitfield", 1), ("Marcus Reed", 2), ("Priya Raman", 3)]
    assert all(c["rank_score"] is not None for c in acme.contacts)


def test_only_qualified_companies_are_searched(runner, universe, now, make_job):
    """Rejected and review companies publish pages too; none may be fetched."""
    review = make_job(company="Mystery Mfg", title="Welder", employees=None,
                      industry="Manufacturing", company_website="https://mysterymfg.com",
                      external_id="mystery-1")
    runner.access.pages["mysterymfg.com"] = TEAM_PAGE
    report = runner.run(raw_jobs=universe + [review], now=now)

    [pending] = report.needs_review
    assert pending.company_name == "mystery mfg"
    assert pending.contact_discovery == "NOT_RUN" and pending.contacts == []
    assert runner.access.requested, "the qualified company is searched"
    assert not any("mysterymfg.com" in url or url == review.application_url
                   for url in runner.access.requested)
    assert report.contact_discovery["qualified"] == 1
    assert report.contact_discovery["searched"] == 1


def test_every_contact_and_email_keeps_its_provenance(runner, universe, now, recording_repo):
    runner.run(raw_jobs=universe, now=now)

    contacts = recording_repo.calls["contact"]
    assert {c["name"] for c in contacts} == {"Jane Whitfield", "Marcus Reed", "Priya Raman"}
    assert all(c["raw_payload"]["page_url"] and c["raw_payload"]["extraction"]
               and c["discovery_stage"] and c["source_type"] for c in contacts)

    evidence = recording_repo.calls["evidence"]
    contact_rows = [e for e in evidence if e["record_type"] == "CONTACT"]
    assert len(contact_rows) == 3 and all(e["url"] and e["record_id"] for e in contact_rows)

    [jane] = [e for e in evidence if e.get("key") == "company_email"]
    assert jane["value"] == "jane@acme-mfg.com"
    assert jane["raw_payload"]["email_type"] == "PERSONAL"
    assert jane["raw_payload"]["contact_name"] == "Jane Whitfield"
    assert jane["raw_payload"]["on_company_domain"] is True
    assert jane["url"]


def test_emails_already_on_postings_are_captured_first(settings, recording_repo, make_job, now):
    job = make_job(company="Acme Manufacturing", title="Plant Manager",
                   employees="51 to 200", industry="Manufacturing",
                   company_website="https://acme-mfg.com",
                   description="Apply to careers@acme-mfg.com or call.",
                   external_id="acme-post")
    runner = PipelineRunner(settings=settings, repo=recording_repo,
                            access=StubAccess({}, settings=settings))
    report = runner.run(raw_jobs=[job], now=now)

    [lead] = report.qualified
    assert lead.role_mailboxes == ["careers@acme-mfg.com"]
    assert lead.email_status == "ROLE_EMAIL_FOUND"
    [row] = [e for e in recording_repo.calls["evidence"] if e.get("key") == "company_email"]
    assert row["raw_payload"]["email_type"] == "ROLE"
    assert row["raw_payload"]["discovery_stage"] == "SAME_SOURCE"
    assert row["raw_payload"]["source"] == "indeed"
    assert row["raw_payload"]["extraction_method"] == "job_posting"
    assert row["url"] == job.application_url


def test_the_company_budget_caps_searches_best_first(settings, recording_repo, make_job, now):
    settings.contacts_max_companies_per_run = 1
    jobs = [make_job(company="Busy Co", title=t, employees="51 to 200",
                     industry="Manufacturing", company_website="https://busyco.com",
                     external_id=f"busy-{t}") for t in ("Welder", "Machinist")]
    jobs.append(make_job(company="Quiet Co", title="Welder", employees="51 to 200",
                         industry="Manufacturing", company_website="https://quietco.com",
                         external_id="quiet-1"))
    access = StubAccess({}, settings=settings)
    report = PipelineRunner(settings=settings, repo=recording_repo, access=access).run(
        raw_jobs=jobs, now=now)

    status = {lead.display_name: lead.contact_discovery for lead in report.qualified}
    assert status == {"Busy Co": "COMPLETED", "Quiet Co": "SKIPPED_COMPANY_BUDGET"}
    assert not any("quietco.com" in url for url in access.requested)
    assert report.contact_discovery["skipped_budget"] == 1


def test_stop_at_before_contacts_searches_nothing(runner, universe, now):
    report = runner.run(raw_jobs=universe, now=now, stop_at="before_contacts")
    assert report.qualified
    assert all(lead.contact_discovery == "NOT_RUN" for lead in report.qualified)
    assert runner.access.requested == []


# ---------------------------------------------------------------------------
# 7. no paid provider, nothing sent
# ---------------------------------------------------------------------------
def test_acceptance_7_no_paid_calls_and_nothing_sent(runner, universe, now, recording_repo):
    runner.run(raw_jobs=universe, now=now)
    for table in ("enrichment_log", "email_verification", "approval", "outreach", "suppression"):
        assert table not in recording_repo.calls, f"{table} must not be written"


# ---------------------------------------------------------------------------
# Reporting and safety
# ---------------------------------------------------------------------------
def test_every_stage_is_timed_and_reported(runner, universe, now):
    report = runner.run(raw_jobs=universe, now=now)
    assert report.run_key
    assert list(report.stages) == [
        "discovery", "normalization", "job_deduplication", "freshness",
        "company_identification", "qualification", "persistence", "contact_discovery",
        "enrichment",
    ]
    assert all(s["status"] == "OK" and s["duration_ms"] >= 0 for s in report.stages.values())
    assert report.to_dict()["stages"] == report.stages


def test_report_json_is_bounded_by_default(runner, universe, now):
    report = runner.run(raw_jobs=universe, now=now)
    payload = report.to_dict()
    assert "raw_jobs_detail" not in payload
    assert "normalized_jobs_detail" not in payload
    assert payload["meta"]["raw_jobs"] == 7
    assert "raw_jobs_detail" in report.to_dict(include_records=True)


def test_dry_run_writes_nothing(settings, universe, now):
    from nexbase.db.repository import InertRepository
    from tests.conftest import RecordingRepo

    repo = RecordingRepo()
    access = StubAccess({"acme-mfg.com": TEAM_PAGE}, settings=settings)
    runner = PipelineRunner(settings=settings, repo=repo, access=access)
    runner.run(raw_jobs=universe, now=now, persist=False)
    assert repo.calls == {}


def test_empty_input_is_safe(runner, now):
    report = runner.run(raw_jobs=[], now=now)
    assert report.raw_jobs == 0
    assert report.qualified == []


def test_dicts_accepted_as_raw_jobs(runner, now):
    """The API passes dicts; the runner must convert them."""
    report = runner.run(
        raw_jobs=[
            {
                "source_type": "JOB_BOARD",
                "source_priority": 2,
                "company": "Dict Co",
                "title": "Plant Manager",
                "date_posted": "2026-05-31",
                "company_url_direct": "https://dictco.com",
                "company_num_employees": "51 to 200",
                "company_industry": "Manufacturing",
            }
        ],
        now=now,
    )
    assert report.raw_jobs == 1
    assert report.companies == 1


def test_a_failing_stage_is_recorded_and_reraised():
    import structlog

    from nexbase.logging_setup import bind_run, get_logger, stage

    metrics: dict = {}
    merge = [structlog.contextvars.merge_contextvars]
    with structlog.testing.capture_logs(processors=merge) as logs, bind_run("run-1"):
        with pytest.raises(RuntimeError):
            with stage(get_logger("t"), "qualification", metrics):
                raise RuntimeError("boom")
    assert metrics["qualification"]["status"] == "FAILED"
    assert metrics["qualification"]["error"] == "boom"
    assert [e["event"] for e in logs] == ["stage_start", "stage_failed"]
    assert all(e["run_id"] == "run-1" for e in logs)
