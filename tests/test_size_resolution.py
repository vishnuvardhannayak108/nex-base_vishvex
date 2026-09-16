"""Employee-size resolution: precedence, cache, web resolver, re-qualification."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nexbase.access.fetcher import FetchedPage
from nexbase.core.enums import QualificationStatus
from nexbase.pipeline.runner import PipelineRunner
from nexbase.pipeline.size_resolver import (
    NullSizeResolver,
    SizeCache,
    SizeEvidence,
    WebSizeResolver,
    extract_employee_size,
)
from tests.test_pipeline_e2e import StubAccess


# ---------------------------------------------------------------------------
# Explicit-evidence extraction. Never infers.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("We have 50-200 employees across three plants.", (50, 200)),
        ("A team of 120 keeps the line running.", (120, 120)),
        ("Over 500 employees worldwide.", (500, None)),
        ("500+ employees", (500, None)),
        ("fewer than 50 employees", (None, 50)),
        ("The company employs 85 people.", (85, 85)),
        ("Our staff of 42 is based in Ohio.", (42, 42)),
    ],
)
def test_explicit_statements_are_extracted(text, expected):
    got = extract_employee_size(text)
    assert got is not None
    assert (got[0], got[1]) == expected
    assert got[2]


@pytest.mark.parametrize(
    "text",
    [
        "Annual revenue of 200 million dollars.",
        "We operate 45 locations nationwide.",
        "Our facility spans 200,000 square feet.",
        "Follow our 50,000 followers on LinkedIn.",
        "Serving 200 clients since 1998.",
        "Founded 40 years ago.",
        "We ship 500 units per day.",
        "We are hiring 30 roles this quarter.",
        "",
        None,
    ],
)
def test_non_headcount_numbers_are_never_read_as_size(text):
    """Revenue, locations, square footage, followers, hiring volume: not headcount."""
    assert extract_employee_size(text) is None


def test_implausible_values_rejected():
    assert extract_employee_size("We have 99,000,000 employees") is None


# ---------------------------------------------------------------------------
# Web resolver
# ---------------------------------------------------------------------------
ABOUT_PAGE = (
    "<html><body><h1>About Acme</h1><p>"
    + "Acme has been fabricating since 1974. " * 20
    + "Today we employ 140 people at two plants.</p></body></html>"
)
NO_SIZE_PAGE = (
    "<html><body><h1>About</h1><p>"
    + "We build quality products for our customers. " * 30
    + "</p></body></html>"
)


def _resolver(pages, settings, **kw):
    return WebSizeResolver(
        access=StubAccess(pages, settings=settings), respect_robots=False, **kw
    )


def test_web_resolver_success(settings):
    evidence = _resolver({"acme.com/about": ABOUT_PAGE}, settings).resolve(
        "acme.com", "acme"
    )
    assert evidence is not None
    assert (evidence.employee_size_min, evidence.employee_size_max) == (140, 140)
    assert evidence.source == "PUBLIC_WEB"
    assert evidence.url and evidence.snippet


def test_web_resolver_failure_returns_none(settings):
    assert _resolver({"acme.com/about": NO_SIZE_PAGE}, settings).resolve(
        "acme.com", "acme"
    ) is None


def test_web_resolver_without_domain_does_nothing(settings):
    assert _resolver({}, settings).resolve(None, "acme") is None


def test_web_resolver_respects_page_budget(settings):
    resolver = _resolver({}, settings, max_pages=2)
    resolver.resolve("acme.com", "acme")
    assert resolver.pages_fetched <= 2


def test_web_resolver_honours_robots(settings):
    pages = {
        "acme.com/robots.txt": "User-agent: *\nDisallow: /about",
        "acme.com/about": ABOUT_PAGE,
    }
    resolver = WebSizeResolver(
        access=StubAccess(pages, settings=settings), respect_robots=True
    )
    assert resolver.resolve("acme.com", "acme") is None


def test_null_resolver_is_the_default_noop():
    assert NullSizeResolver().resolve("acme.com", "acme") is None


# ---------------------------------------------------------------------------
# Cache: 90-day window, reuses companies.updated_at
# ---------------------------------------------------------------------------
class _CacheRepo:
    configured = True

    def __init__(self, row):
        self.row = row

    def find_company(self, domain, name):
        return self.row


def _row(days_old, lo=90, hi=90):
    stamp = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    return {"employee_size_min": lo, "employee_size_max": hi,
            "employee_size_source": "PUBLIC_WEB", "updated_at": stamp}


def test_cached_size_within_window_is_reused():
    cache = SizeCache(repo=_CacheRepo(_row(10)), max_age_days=90)
    evidence = cache.lookup("acme.com", "acme")
    assert evidence is not None and evidence.employee_size_min == 90
    assert cache.hits == 1


def test_cached_size_expires_at_90_days():
    assert SizeCache(repo=_CacheRepo(_row(91)), max_age_days=90).lookup(
        "acme.com", "acme") is None
    assert SizeCache(repo=_CacheRepo(_row(89)), max_age_days=90).lookup(
        "acme.com", "acme") is not None


def test_cache_ignores_rows_without_size():
    row = _row(1)
    row["employee_size_min"] = row["employee_size_max"] = None
    assert SizeCache(repo=_CacheRepo(row)).lookup("acme.com", "acme") is None


def test_cache_is_inert_without_a_database():
    assert SizeCache(repo=None).lookup("acme.com", "acme") is None


# ---------------------------------------------------------------------------
# Precedence and end-to-end re-qualification
# ---------------------------------------------------------------------------
class FixedResolver:
    """Returns a fixed size for every company; counts invocations."""

    def __init__(self, lo, hi):
        self.lo, self.hi, self.calls = lo, hi, []

    def resolve(self, domain, company_name):
        self.calls.append(company_name)
        if self.lo is None and self.hi is None:
            return None
        return SizeEvidence(self.lo, self.hi, "PUBLIC_WEB",
                            url=f"https://{domain}/about", snippet="employs people")


def _runner(settings, repo, resolver):
    return PipelineRunner(settings=settings, repo=repo,
                          access=StubAccess({}, settings=settings),
                          size_resolver=resolver)


def _sizeless(make_job, name="Mystery Mfg"):
    """A company no job board gave a headcount for."""
    return [make_job(company=name, title=t, days_old=1, employees=None,
                     industry="Industrial Manufacturing",
                     company_website=f"https://{name.lower().replace(' ', '')}.com",
                     description="Rapidly growing, new facility.", external_id=f"{name}-{t}")
            for t in ("Machinist", "Welder", "Press Operator")]


def test_unknown_size_resolved_to_in_range_becomes_qualified(
    settings, recording_repo, make_job, now
):
    resolver = FixedResolver(120, 120)
    report = _runner(settings, recording_repo, resolver).run(
        raw_jobs=_sizeless(make_job), now=now)

    assert resolver.calls, "the blocked company must reach size resolution"
    assert report.size_resolution["resolved"] == 1
    assert report.size_resolution["in_range"] == 1
    assert report.size_resolution["newly_qualified"] == 1
    assert [l.company_name for l in report.qualified] == ["mystery mfg"]


def test_oversize_found_by_the_resolver_is_rejected(
    settings, recording_repo, make_job, now
):
    """The verdict must not depend on WHERE the employee size came from."""
    report = _runner(settings, recording_repo, FixedResolver(5000, 9000)).run(
        raw_jobs=_sizeless(make_job), now=now)

    assert report.size_resolution["oversize"] == 1
    assert report.size_resolution["newly_rejected"] == 1
    assert report.rejected[0].reasons == ["EMPLOYEE_SIZE_ABOVE_MAX"]
    assert report.size_resolution["newly_qualified"] == 0
    assert report.needs_review == []
    rejected = {r.company_name: r for r in report.rejected}
    assert "EMPLOYEE_SIZE_ABOVE_MAX" in rejected["mystery mfg"].reasons


def test_unknown_size_resolved_to_undersize_is_rejected(
    settings, recording_repo, make_job, now
):
    report = _runner(settings, recording_repo, FixedResolver(4, 4)).run(
        raw_jobs=_sizeless(make_job), now=now)

    assert report.size_resolution["undersize"] == 1
    assert report.size_resolution["newly_rejected"] == 1
    rejected = {r.company_name: r for r in report.rejected}
    assert "EMPLOYEE_SIZE_BELOW_MIN" in rejected["mystery mfg"].reasons


def test_unresolved_size_remains_needs_review(
    settings, recording_repo, make_job, now
):
    report = _runner(settings, recording_repo, FixedResolver(None, None)).run(
        raw_jobs=_sizeless(make_job), now=now)

    assert report.size_resolution["unresolved"] == 1
    assert report.size_resolution["newly_qualified"] == 0
    assert "EMPLOYEE_SIZE_UNKNOWN" in report.needs_review[0].review_flags


def test_rejected_companies_never_enter_size_resolution(
    settings, recording_repo, make_job, now
):
    """Only companies blocked ONLY on unknown size are resolved."""
    resolver = FixedResolver(120, 120)
    jobs = [
        make_job(company="Bolt Staffing Group", title="Recruiter", days_old=1,
                 company_website="https://boltstaffing.com", external_id="b1"),
        make_job(company="Tiny Shop", title="Machinist", days_old=1,
                 employees="1 to 10", industry="Manufacturing",
                 company_website="https://tinyshop.com", external_id="t1"),
    ]
    _runner(settings, recording_repo, resolver).run(
        raw_jobs=jobs, now=now)

    assert resolver.calls == [], f"resolved rejected companies: {resolver.calls}"


def test_indeed_observed_size_still_wins_without_resolution(
    settings, recording_repo, make_job, now
):
    """A board-supplied headcount needs no resolution at all."""
    resolver = FixedResolver(999, 999)
    jobs = [make_job(company="Acme Mfg", title=t, days_old=1, employees="51 to 200",
                     industry="Industrial Manufacturing",
                     company_website="https://acmemfg.com", external_id=t)
            for t in ("Machinist", "Welder", "Press Operator")]
    report = _runner(settings, recording_repo, resolver).run(
        raw_jobs=jobs, now=now)

    assert resolver.calls == []
    lead = (report.qualified + report.needs_review)[0]
    assert lead.employee_size == "51-200"
    assert lead.employee_size_source == "JOB_BOARD"


def test_explicit_provided_profile_beats_everything(
    settings, recording_repo, make_job, now
):
    from nexbase.pipeline.profile import CompanyProfile

    resolver = FixedResolver(999, 999)
    jobs = _sizeless(make_job)
    explicit = CompanyProfile(employee_size_min=60, employee_size_max=60,
                              size_known=True, size_source="MANUAL",
                              client_industry="Manufacturing", industry_known=True)
    report = _runner(settings, recording_repo, resolver).run(
        raw_jobs=jobs, now=now,
        profiles={("mysterymfg.com", "mystery mfg"): explicit})

    assert resolver.calls == []
    lead = (report.qualified + report.needs_review)[0]
    assert lead.employee_size_source == "MANUAL"


def test_cached_size_is_used_before_the_web_resolver(
    settings, make_job, now
):
    """Cross-run persistence: a verified size is reused, not re-scraped."""
    from tests.conftest import RecordingRepo

    class CachedRepo(RecordingRepo):
        def find_company(self, domain, name):
            return _row(5, lo=75, hi=75) | {"persistent_hiring_runs": 2}

    resolver = FixedResolver(999, 999)
    report = _runner(settings, CachedRepo(), resolver).run(
        raw_jobs=_sizeless(make_job), now=now)

    assert resolver.calls == [], "cache hit must prevent a web fetch"
    lead = (report.qualified + report.needs_review)[0]
    assert lead.employee_size == "75"


def test_size_band_is_not_relaxed(settings):
    assert (settings.size_filter_min, settings.size_filter_max) == (11, 200)


def test_resolver_disabled_leaves_unknown_as_needs_review(
    settings, recording_repo, make_job, now
):
    off = settings.model_copy(update={"size_resolver_enabled": False})
    report = PipelineRunner(settings=off, repo=recording_repo,
                            access=StubAccess({}, settings=off)).run(
        raw_jobs=_sizeless(make_job), now=now)

    assert report.size_resolution["resolved"] == 0
    assert "EMPLOYEE_SIZE_UNKNOWN" in report.needs_review[0].review_flags
