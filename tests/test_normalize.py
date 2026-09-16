"""Normalization, and specifically the domain-resolution rules."""
from __future__ import annotations

import pytest

from nexbase.core.models import RawJob
from nexbase.pipeline.normalize import (
    domain_from_email,
    is_employer_domain,
    normalize_company_name,
    normalize_domain,
    normalize_text,
    registrable_domain,
    resolve_domain,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Acme Manufacturing Inc.", "acme manufacturing"),
        ("Acme Manufacturing, LLC", "acme manufacturing"),
        ("Acme  Manufacturing   Co., Inc.", "acme manufacturing"),
        ("Echo Corp", "echo"),
        ("Summit Construction Ltd.", "summit construction"),
        ("Widgets GmbH", "widgets"),
        (None, ""),
        ("   ", ""),
    ],
)
def test_normalize_company_name(raw, expected):
    assert normalize_company_name(raw) == expected


def test_normalize_text_collapses_whitespace():
    assert normalize_text("  Plant   Manager\n") == "plant manager"


@pytest.mark.parametrize(
    "host,expected",
    [
        ("www.acme.com", "acme.com"),
        ("jobs.acme.co.uk", "acme.co.uk"),
        ("careers.sub.acme.com", "acme.com"),
        ("acme.com", "acme.com"),
    ],
)
def test_registrable_domain(host, expected):
    assert registrable_domain(host) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://www.indeed.com/cmp/Acme-Manufacturing",
        "https://www.linkedin.com/company/acme",
        "https://boards.greenhouse.io/acme",
        "https://jobs.lever.co/acme",
        "https://acme.myworkdayjobs.com/careers",
        "https://www.ziprecruiter.com/c/Acme",
        "https://gmail.com",
    ],
)
def test_platform_urls_never_yield_a_domain(url):
    """The bug that collapsed every Indeed company onto `indeed.com`."""
    assert normalize_domain(url) == ""
    assert is_employer_domain(normalize_domain(url) or "indeed.com") is False


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.acme-manufacturing.com/careers", "acme-manufacturing.com"),
        ("http://acme.co.uk", "acme.co.uk"),
        ("acme.com", "acme.com"),
    ],
)
def test_employer_urls_yield_a_domain(url, expected):
    assert normalize_domain(url) == expected


def test_domain_from_email():
    assert domain_from_email("jane@acme.com") == "acme.com"
    assert domain_from_email("jane@gmail.com") == ""
    assert domain_from_email("not-an-email") == ""


def test_resolve_domain_prefers_company_website():
    job = RawJob(
        source_type="JOB_BOARD",
        source_priority=2,
        company_url="https://www.indeed.com/cmp/Acme",
        company_website="https://www.acme.com",
        application_url="https://www.indeed.com/viewjob?jk=1",
    )
    assert resolve_domain(job) == ("acme.com", "COMPANY_WEBSITE")


def test_resolve_domain_falls_back_to_observed_email():
    job = RawJob(
        source_type="JOB_BOARD",
        source_priority=2,
        company_url="https://www.indeed.com/cmp/Acme",
        observed_emails=["hiring@acme-industrial.com"],
    )
    assert resolve_domain(job) == ("acme-industrial.com", "OBSERVED_EMAIL")


def test_resolve_domain_returns_none_rather_than_guessing():
    job = RawJob(
        source_type="ATS",
        source_priority=1,
        company_name="Foxtrot Metals",
        application_url="https://boards.greenhouse.io/foxtrot/jobs/1",
    )
    assert resolve_domain(job) == ("", "NONE")


def test_normalize_job_populates_evidence_url(make_job):
    from nexbase.pipeline.normalize import normalize_job

    job = normalize_job(make_job(company_website="https://acme.com"))
    assert job.domain == "acme.com"
    assert job.evidence_url is not None
    assert job.posting_date is not None


# ---------------------------------------------------------------------------
# Regressions found by a live Indeed run
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad",
    [
        "indeed.comnone", "https://www.indeed.comnone",
        "linkedin.comnull", "glassdoor.comundefined",
        "no-dot-host", "acme.123", "acme.",
    ],
)
def test_corrupted_hosts_are_rejected(bad):
    """Live Indeed data contained a literal host 'indeed.comnone'."""
    assert normalize_domain(bad) == ""


@pytest.mark.parametrize(
    "good",
    [
        "https://acme.tools", "https://summit.builders", "https://acme.farm",
        "https://growers.coop", "https://acme.aero", "https://acme.plumbing",
        "https://acme.industries", "https://acme.co.uk",
    ],
)
def test_modern_employer_tlds_are_kept(good):
    """A hand-written TLD allowlist silently deleted real employer domains."""
    assert normalize_domain(good) != ""


@pytest.mark.parametrize(
    "ats_url",
    [
        "http://recruit.hirebridge.com/v3/Jobs/JobDetails.aspx?jid=1",
        "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/x.html",
        "https://acme.applicantpro.com/jobs/1",
        "https://apply.workable.com/acme/j/ABC",
        "https://acme.isolvedhire.com/jobs/2",
    ],
)
def test_long_tail_ats_hosts_rejected(ats_url):
    assert normalize_domain(ats_url) == ""


def test_valid_employer_domains_still_pass():
    assert normalize_domain("https://www.toledotool.com/careers") == "toledotool.com"
    assert normalize_domain("https://kraftheinz.com") == "kraftheinz.com"
    assert normalize_domain("https://acme.co.uk/about") == "acme.co.uk"


def test_nameless_posting_is_not_actionable(make_job):
    from nexbase.pipeline.normalize import Normalizer, normalize_job

    nameless = normalize_job(make_job(company=None))
    assert nameless.is_actionable is False

    actionable, discarded = Normalizer().normalize(
        [make_job(company="Acme Inc."), make_job(company=None)]
    )
    assert len(actionable) == 1
    assert len(discarded) == 1


@pytest.mark.parametrize(
    "aggregator_url",
    [
        "https://www.postjobfree.com/job/dhvc5h/warehouse-associate-columbus-oh",
        "https://us.jobrapido.com/jobpreview/1",
        "https://www.careerjet.com/jobad/us123",
        "https://us.jora.com/job/1",
        "https://www.nexxt.com/jobs/job/view?url=x",
        "https://www.talent.com/view?id=1",
        "https://www.simplyhired.com/job/abc",
    ],
)
def test_aggregator_hosts_never_become_employer_domains(aggregator_url):
    """An aggregator serves job URLs on its own host; that is not the employer."""
    assert normalize_domain(aggregator_url) == ""
