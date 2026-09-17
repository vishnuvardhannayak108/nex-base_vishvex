"""Discovery: taxonomy, JobSpy mapping, ATS guardrails, board scrapers."""
from __future__ import annotations

import pytest

from nexbase.core.enums import ClientIndustry
from nexbase.core.errors import DiscoveryError
from nexbase.discovery import taxonomy
from nexbase.discovery.ats_discovery import ATSDiscovery
from nexbase.discovery.board_scrapers import (
    PostJobFreeDiscovery,
    SimplyHiredDiscovery,
    TalentComDiscovery,
    parse_relative_age,
)
from nexbase.discovery.jobspy_discovery import SUPPORTED_SITES, JobSpyDiscovery
from nexbase.discovery.linkedin_signal import parse_applicant_count


# ---------------------------------------------------------------------------
# Taxonomy (derived from the BLS/O*NET data in the repo)
# ---------------------------------------------------------------------------
def test_taxonomy_covers_all_ten_client_industries():
    industries = set(taxonomy.industries())
    expected = {i.value for i in ClientIndustry}
    assert expected == industries


def test_taxonomy_has_a_substantial_title_universe():
    terms = taxonomy.load_search_terms()
    assert len(terms) > 5000
    assert len({t.term for t in terms}) > 2000


def test_no_fixed_job_title_list_is_hardcoded():
    """Brief: 'Do not restrict discovery to a fixed job-title list.'

    The universe is data-derived and per-industry. Term sets are *not*
    disjoint - a welder or an insulator genuinely appears under more than one
    industry - so the property under test is that each industry draws from its
    own sizeable pool, not that the pools never intersect.
    """
    by_industry = taxonomy.terms_by_industry()
    manufacturing = {t.term for t in by_industry["Manufacturing"]}
    construction = {t.term for t in by_industry["Construction"]}

    assert len(by_industry["Manufacturing"]) > 500
    assert len(by_industry["Construction"]) > 500
    # Mostly different, even though some titles legitimately overlap.
    assert len(manufacturing & construction) < len(manufacturing) / 2


def test_blue_and_white_collar_both_present():
    terms = {t.term.lower() for t in taxonomy.load_search_terms()}
    blue = {"welder", "machinist", "electrician", "forklift operator", "carpenter"}
    white = {"accountant", "buyer", "civil engineer", "architect"}
    assert blue & terms, "no blue-collar titles"
    assert white & terms, "no white-collar titles"


def test_industry_reverse_lookup():
    """Lookups resolve to a client industry via the BLS SOC->NAICS mapping.

    The exact industry is whatever the mapping says (welders sit under
    Industrial Equipment/Machinery, not generic Manufacturing) - the contract
    is that a known title maps somewhere valid and an unknown one maps nowhere.
    """
    valid = {i.value for i in ClientIndustry}
    assert taxonomy.industry_for_title("Welder") in valid
    assert taxonomy.industry_for_title("Carpenter") in valid
    assert taxonomy.industry_for_title("Zebra Wrangler") is None


# ---------------------------------------------------------------------------
# JobSpy mapping
# ---------------------------------------------------------------------------
def test_monster_and_simplyhired_are_not_jobspy_sites():
    """Verified against the installed package: JobSpy has no such scrapers."""
    assert "monster" not in SUPPORTED_SITES
    assert "simplyhired" not in SUPPORTED_SITES


def test_jobspy_row_separates_board_profile_from_real_website(settings):
    row = {
        "id": "in-123",
        "site": "indeed",
        "title": "Plant Manager",
        "company": "Acme Manufacturing",
        "company_url": "https://www.indeed.com/cmp/Acme-Manufacturing",
        "company_url_direct": "https://www.acme-mfg.com",
        "company_industry": "Manufacturing",
        "company_num_employees": "51 to 200",
        "emails": "hr@acme-mfg.com, jane@acme-mfg.com",
        "job_url": "https://www.indeed.com/viewjob?jk=123",
        "job_url_direct": "https://acme-mfg.com/careers/1",
        "location": "Columbus, OH",
        "date_posted": "2026-05-30",
        "description": "desc",
        "is_remote": False,
    }
    job = JobSpyDiscovery(settings)._to_raw_job(row)

    assert job.company_url == "https://www.indeed.com/cmp/Acme-Manufacturing"
    assert job.company_website == "https://www.acme-mfg.com"
    assert job.company_industry == "Manufacturing"
    assert job.company_employee_count == "51 to 200"
    assert job.observed_emails == ["hr@acme-mfg.com", "jane@acme-mfg.com"]
    assert job.source_site == "indeed"
    assert job.country is None  # JobSpy emits no country column


def test_jobspy_nan_values_become_none(settings):
    row = {"id": "1", "site": "indeed", "title": "X", "company": "Y",
           "company_url": float("nan"), "description": "  "}
    job = JobSpyDiscovery(settings)._to_raw_job(row)
    assert job.company_url is None
    assert job.description is None


def test_unsupported_sites_are_filtered(settings, caplog):
    jobspy = JobSpyDiscovery(settings)
    result = jobspy.search(["welder"], ["Ohio"], site_names=["monster", "simplyhired"])
    assert result == []


# ---------------------------------------------------------------------------
# ATS guardrails
# ---------------------------------------------------------------------------
def test_full_snapshot_refused_without_optin(settings):
    from nexbase.config import Settings

    strict = Settings(ats_allow_full_snapshot=False)
    with pytest.raises(DiscoveryError, match="16.9 GB"):
        ATSDiscovery(strict)._resolve_slices(None)


def test_explicit_slice_respected(settings):
    assert ATSDiscovery(settings)._resolve_slices("greenhouse") == ["greenhouse"]


def test_ats_row_has_no_fabricated_domain(settings):
    row = {
        "global_id": "greenhouse:1", "title": "Welder", "company": "Foxtrot Metals",
        "ats_type": "greenhouse", "url": "https://boards.greenhouse.io/foxtrot/jobs/1",
        "location": "Toledo, OH", "posted_at": "2026-05-30",
    }
    job = ATSDiscovery(settings)._to_raw_job(row)
    assert job.company_website is None  # the dataset has no such column
    assert job.external_id == "greenhouse:1"
    assert job.ats_platform == "greenhouse"


# ---------------------------------------------------------------------------
# Board adapters: schema.org JobPosting
# ---------------------------------------------------------------------------
JOBPOSTING_HTML = """
<html><body>
<script type="application/ld+json">
{"@context":"https://schema.org/","@type":"JobPosting",
 "title":"Maintenance Technician","datePosted":"2026-05-29",
 "hiringOrganization":{"@type":"Organization","name":"Toledo Plastics",
   "sameAs":"https://toledoplastics.com"},
 "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress",
   "addressLocality":"Toledo","addressRegion":"OH"}},
 "url":"https://www.example-board.com/job-openings/abc",
 "identifier":{"@type":"PropertyValue","value":"abc"},
 "description":"<p>Maintain production lines.</p>"}
</script>
</body></html>
"""


@pytest.mark.parametrize("cls", [SimplyHiredDiscovery, TalentComDiscovery, PostJobFreeDiscovery])
def test_board_scrapers_parse_jsonld(cls):
    jobs = cls().parse(JOBPOSTING_HTML, "https://example.com/search")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Maintenance Technician"
    assert job.company_name == "Toledo Plastics"
    assert job.company_website == "https://toledoplastics.com"
    assert job.location == "Toledo, OH"
    assert job.posted_at is not None
    assert job.source_site == cls.config.site


def test_board_search_urls_well_formed():
    simply = SimplyHiredDiscovery().search_url("plant manager", "Toledo, OH")
    assert "simplyhired.com" in simply and "plant+manager" in simply


@pytest.mark.parametrize("cls", [SimplyHiredDiscovery, TalentComDiscovery, PostJobFreeDiscovery])
def test_board_scraper_returns_nothing_for_empty_html(cls):
    assert cls().parse("", "https://x") == []


@pytest.mark.parametrize(
    "text,expect_none",
    [("Posted 3 days ago", False), ("Just posted", False),
     ("2 hours ago", False), ("", True), ("Posted recently", True)],
)
def test_parse_relative_age(text, expect_none):
    assert (parse_relative_age(text) is None) is expect_none


# ---------------------------------------------------------------------------
# LinkedIn applicant signal
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "html,expected",
    [
        ('<span class="num-applicants__caption">17 applicants</span>', 17),
        ('<figcaption class="num-applicants__caption">Over 200 applicants</figcaption>', 200),
        ('<span class="num-applicants__caption">Be among the first 25 applicants</span>', 25),
    ],
)
def test_parse_applicant_count(html, expected):
    signal = parse_applicant_count(html)
    assert signal is not None
    assert signal.applicant_count == expected


def test_missing_applicant_count_returns_none_not_a_guess():
    assert parse_applicant_count("<html><body>no data</body></html>") is None
    assert parse_applicant_count("") is None


# ---------------------------------------------------------------------------
# Term searchability — a live run wasted 6 of 8 probes on unsearchable titles
# like "Ladle Liner", "Charter Pilot" and "Retort Operator".
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "term,ok",
    [
        ("Plant Manager", True),
        ("Welder", True),
        ("Order Picker", True),
        ("Stockers and Order Fillers", False),   # BLS label, plural + "and"
        ("Packers and Packagers, Hand", False),  # comma clause
        ("Aerospace Engineers", False),          # plural head noun
        ("Managers, All Other", False),
        ("Architects, Except Landscape and Naval", False),
    ],
)
def test_is_searchable(term, ok):
    assert taxonomy.is_searchable(term) is ok


def test_suggested_terms_are_all_searchable():
    for industry in taxonomy.industries():
        for term in taxonomy.suggest_terms_for_sector(industry, count=20):
            assert taxonomy.is_searchable(term), f"{industry}: {term!r}"


def test_suggestions_avoid_bls_labels():
    """Rank 0 is the canonical BLS label and searches badly."""
    suggested = set(taxonomy.suggest_terms_for_sector("Warehousing & Distribution"))
    assert "Stockers and Order Fillers" not in suggested
    assert "Conveyor Operators and Tenders" not in suggested


# Live check 2026-09-14: SimplyHired stamps cards "4d", not "4 days ago".
# The long form only cost us every date on the board.
@pytest.mark.parametrize(
    "text,days",
    [("4d", 4), ("3 days ago", 3), ("2w", 14), ("5h", 0), ("30m", 0),
     ("1mo", 30), ("30+ days ago", 30), ("Just posted", 0)],
)
def test_compact_relative_ages_parse(text, days):
    from datetime import datetime, timezone
    now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    got = parse_relative_age(text, now=now)
    assert got is not None
    assert round((now - got).total_seconds() / 86400) == days


@pytest.mark.parametrize("text", ["", "xx", "Posted recently", None])
def test_unparseable_age_is_none_not_a_guess(text):
    assert parse_relative_age(text) is None


def test_monster_is_gone_after_live_failure():
    """It fetched fine but served no job rows; a silent-zero source is not kept."""
    from nexbase.config import Settings
    from nexbase.discovery.board_scrapers import BOARD_SCRAPERS
    from nexbase.discovery.registry import build_registry

    assert "monster" not in BOARD_SCRAPERS
    assert build_registry(Settings(_env_file=None)).get("monster") is None


# ---------------------------------------------------------------------------
# Talent.com — added after the 2026-09-14 coverage probe. Server-renders an
# exact ISO timestamp, which is why it was chosen over 8 other US candidates.
# ---------------------------------------------------------------------------
TALENT_CARD = """
<html><body>
<div data-testid="job-card-unified">
  <a href="/view?id=635461178950684215"></a>
  <h2 class="JobCard_title__X32Qk">Warehouse Associate</h2>
  <span class="JobCard_company__NmRol">American Freight</span>
  <span class="JobCard_location__nmTtw">Columbus, US</span>
  <time datetime="2026-09-02T04:38:32Z">Last updated: 11 days ago</time>
</div>
</body></html>
"""


def test_talent_com_extracts_exact_timestamp():
    from nexbase.discovery.board_scrapers import TalentComDiscovery

    jobs = TalentComDiscovery().parse(TALENT_CARD, "https://www.talent.com/jobs?k=x")
    assert len(jobs) == 1
    j = jobs[0]
    assert j.title == "Warehouse Associate"
    assert j.company_name == "American Freight"
    assert j.location == "Columbus, US"
    assert j.source_site == "talent_com"
    assert j.posted_at is not None
    assert j.posted_at.strftime("%Y-%m-%d") == "2026-09-02"
    assert j.application_url.endswith("/view?id=635461178950684215")


def test_talent_com_search_url_paginates():
    from nexbase.discovery.board_scrapers import TalentComDiscovery

    url = TalentComDiscovery().search_url("warehouse associate", "Columbus, OH", page=2)
    assert "talent.com/jobs" in url and "p=2" in url and "warehouse+associate" in url
    assert "radius=100" in url, "a state search is otherwise a 15 mi circle"


def test_enabled_board_scrapers_are_the_verified_ones():
    from nexbase.config import Settings
    from nexbase.discovery.board_scrapers import BOARD_SCRAPERS
    from nexbase.discovery.registry import build_registry

    registry = build_registry(Settings(_env_file=None))
    enabled = {portal for portal in BOARD_SCRAPERS if registry.get(portal).enabled}
    assert enabled == {"simplyhired", "talent_com", "postjobfree"}


# ---------------------------------------------------------------------------
# PostJobFree — added 2026-09-14. Server-renders every field; the date stamp
# omits the year ("Sep 12"), resolved deterministically, never guessed.
# ---------------------------------------------------------------------------
PJF_CARD = """
<html><body>
<div class="snippetPadding">
  <h3 class="itemTitle"><a href="/job/dhvc5h/merchandise-handler-bexley-oh">Merchandise Handler</a></h3>
  <div class="normalText">
    <span class="colorCompany">Bath &amp; Body Works</span>
    <span class="colorLocation">Bexley, OH</span>
    <div><span class="jdSnippet">Operate Warehouse Management System.</span>
         - <span class="colorDate">Sep 12</span></div>
  </div>
</div>
</body></html>
"""


def test_postjobfree_extracts_all_required_fields():
    from nexbase.discovery.board_scrapers import PostJobFreeDiscovery

    jobs = PostJobFreeDiscovery().parse(PJF_CARD, "https://www.postjobfree.com/jobs?q=x")
    assert len(jobs) == 1
    j = jobs[0]
    assert j.title == "Merchandise Handler"
    assert j.company_name == "Bath & Body Works"
    assert j.location == "Bexley, OH"
    assert j.description
    assert j.posted_at is not None and j.posted_at.month == 9 and j.posted_at.day == 12
    assert j.application_url.endswith("/job/dhvc5h/merchandise-handler-bexley-oh")
    assert j.source_site == "postjobfree"


@pytest.mark.parametrize(
    "stamp,month,day,year_offset",
    [("Sep 12", 9, 12, 0), ("Aug 30", 8, 30, 0), ("Jan 3", 1, 3, 0),
     ("Dec 28", 12, 28, -1), ("Sep 20", 9, 20, -1)],
)
def test_month_day_year_is_resolved_not_guessed(stamp, month, day, year_offset):
    """A year-less stamp more than a day ahead must belong to last year."""
    from datetime import datetime, timezone
    from nexbase.discovery.board_scrapers import parse_month_day

    now = datetime(2026, 9, 14, tzinfo=timezone.utc)
    got = parse_month_day(stamp, now=now)
    assert got is not None
    assert (got.month, got.day, got.year) == (month, day, 2026 + year_offset)


@pytest.mark.parametrize("bad", ["", None, "no date here", "Septemberish"])
def test_month_day_returns_none_rather_than_guessing(bad):
    from nexbase.discovery.board_scrapers import parse_month_day

    assert parse_month_day(bad) is None
