"""Qualification: size rule, industry, intermediaries, internal TA, review flags."""
from __future__ import annotations

import pytest

from nexbase.core.enums import QualificationStatus
from nexbase.pipeline.company_identity import prepare_companies
from nexbase.discovery.taxonomy import sector_for_industry_label
from nexbase.pipeline.profile import (
    InternalTASignal,
    build_profile,
    detect_internal_ta,
    parse_employee_range,
)
from nexbase.pipeline.qualification import (
    detect_agency,
    evaluate_internal_ta,
    evaluate_size,
    score_company,
)


def _fresh(jobs, settings, now):
    return prepare_companies(jobs, settings, now)[0]


# ---------------------------------------------------------------------------
# Employee size
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("51 to 200", (51, 200)),
        ("11 to 50", (11, 50)),
        ("1,001 to 5,000", (1001, 5000)),
        ("10,000+", (10000, None)),
        ("more than 500", (500, None)),
        ("fewer than 25", (None, 25)),
        ("200", (200, 200)),
        ("200 employees", (200, 200)),
        ("", (None, None)),
        (None, (None, None)),
        ("unknown", (None, None)),
    ],
)
def test_parse_employee_range(text, expected):
    assert parse_employee_range(text) == expected


def test_size_rule_is_actually_enforced(make_job, settings, now):
    """The band was dead code before; verify all three verdicts fire."""
    in_range = _fresh([make_job(company="Right Size", employees="51 to 200")], settings, now)[0]
    too_big = _fresh([make_job(company="Too Big", employees="1,001 to 5,000")], settings, now)[0]
    too_small = _fresh([make_job(company="Too Small", employees="1 to 9")], settings, now)[0]

    assert evaluate_size(build_profile(in_range), settings)[0] == "IN_RANGE"
    assert evaluate_size(build_profile(too_big), settings)[0] == "OVERSIZE"
    assert evaluate_size(build_profile(too_small), settings)[0] == "UNDERSIZE"


def test_undersize_is_rejected(make_job, settings, now):
    fresh = _fresh([make_job(company="Tiny Co", employees="1 to 9")], settings, now)[0]
    result = score_company(fresh, settings=settings)
    assert result.status == QualificationStatus.REJECTED.value
    assert "EMPLOYEE_SIZE_BELOW_MIN" in result.reasons


def test_oversize_is_rejected(make_job, settings, now):
    """Master Plan: >200 employees is a rejection. There is no approval path."""
    fresh = _fresh(
        [make_job(company="Mega Corp", employees="5,000 to 10,000",
                  industry="Manufacturing", days_old=1)],
        settings, now,
    )[0]
    result = score_company(fresh, settings=settings)
    assert result.status == QualificationStatus.REJECTED.value
    assert result.reasons == ["EMPLOYEE_SIZE_ABOVE_MAX"]


# ---------------------------------------------------------------------------
# Industry
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "label,expected",
    [
        # A label that is a NexBase sector name.
        ("Manufacturing", "Manufacturing"),
        ("Construction", "Construction"),
        ("Retail", "Retail"),
        ("Plastics/Rubber", "Plastics/Rubber"),
        # Official NAICS titles, at any level, through naics_to_industry.csv.
        ("Automotive Dealers", "Retail"),                   # 441, not Manufacturing
        ("Building Materials", "Retail"),                   # 4441 dealers
        ("Motor Vehicle Manufacturing", "Manufacturing"),   # 3361
        ("Industrial Machinery Manufacturing", "Industrial Equipment/Machinery"),  # 3332
        ("Warehousing", "Warehousing & Distribution"),      # 493 beats its parent 48-49
        ("Truck Transportation", "Logistics & Transportation"),
        ("Hotels & Motels", "Hospitality/Hotels"),
        ("Real Estate", "Property Management/Real Estate"),
        # Ambiguous under the taxonomy: equally close to two sectors.
        ("Restaurants & Food Service", None),   # 722 restaurants vs 6242 Community Food Services
        ("Food And Beverages", None),           # F&B Manufacturing vs 445 Food and Beverage Retailers
        ("Industrial Manufacturing", None),     # 325120 Industrial Gas vs 3332 Industrial Machinery
        # Outside every NexBase sector, or no NAICS title at all.
        ("Hospitals and Health Care", None),
        ("Insurance", None),
        ("Automotive", None),
        ("", None),
        (None, None),
    ],
)
def test_an_industry_label_is_placed_by_the_naics_titles(label, expected):
    assert sector_for_industry_label(label) == expected


def test_industry_inferred_from_titles_is_only_a_hint(make_job, settings, now):
    """Title inference is a guess about the employer, so it is never evidence.

    It used to set industry_known=True, which earned the industry bonus and
    cleared the review flag - an insurance company hiring a "Claims Adjuster"
    qualified as Manufacturing.
    """
    fresh = _fresh(
        [make_job(company="Unknown Industry Co", title="Welder", industry=None,
                  description="We are hiring."),
         make_job(company="Unknown Industry Co", title="Machinist", industry=None,
                  description="We are hiring.")],
        settings, now,
    )[0]
    profile = build_profile(fresh)
    assert profile.industry_known is False
    assert profile.industry_state == "INFERRED_HINT"
    assert profile.industry_source == "OCCUPATION_TAXONOMY_HINT"


# ---------------------------------------------------------------------------
# Intermediaries
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    ["Bolt Staffing Group", "Apex Recruiting LLC", "Summit Executive Search",
     "Premier Employment Agency", "Workforce Solutions Inc"],
)
def test_intermediaries_rejected_by_name(name):
    is_agency, evidence = detect_agency(name, "")
    assert is_agency, name
    assert evidence.startswith("NAME:")


def test_agency_self_description_detected():
    is_agency, evidence = detect_agency(
        "Generic Co", "Our client is seeking an experienced plant manager."
    )
    assert is_agency
    assert evidence.startswith("SELF_DESCRIPTION:")


def test_manufacturer_not_falsely_flagged_by_description():
    """'placement' in a real JD used to hard-reject the company."""
    is_agency, _ = detect_agency(
        "Acme Metal Fabrication",
        "Responsible for the placement of components on the assembly line "
        "and staffing the second shift rotation.",
    )
    assert is_agency is False


# ---------------------------------------------------------------------------
# Internal TA filter
# ---------------------------------------------------------------------------
def test_single_hr_manager_is_not_disqualifying():
    """Brief states this explicitly."""
    signal = detect_internal_ta(["HR Manager", "Plant Manager", "Welder"])
    assert signal.ta_role_count == 0
    assert signal.has_senior_ta_leader is False


def test_mature_internal_ta_rejected(make_job, settings, now):
    titles = ["Technical Recruiter", "Talent Acquisition Manager",
              "Senior Sourcer", "Head of Talent Acquisition"]
    fresh = _fresh(
        [make_job(company="BigCo", title=t, employees="51 to 200") for t in titles],
        settings, now,
    )[0]
    result = score_company(fresh, settings=settings)
    assert result.status == QualificationStatus.REJECTED.value
    assert "MATURE_INTERNAL_TA" in result.reasons


def test_borderline_ta_flagged_for_review(make_job, settings, now):
    fresh = _fresh(
        [make_job(company="MidCo", title="Recruiter", employees="51 to 200",
                  industry="Manufacturing"),
         make_job(company="MidCo", title="Recruiting Coordinator",
                  employees="51 to 200", industry="Manufacturing"),
         make_job(company="MidCo", title="Plant Manager", employees="51 to 200",
                  industry="Manufacturing")],
        settings, now,
    )[0]
    result = score_company(fresh, settings=settings)
    # Two TA roles: enough to flag, not enough to reject outright.
    assert result.status == QualificationStatus.NEEDS_REVIEW.value
    assert "POSSIBLE_INTERNAL_TA" in result.review_flags


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def test_fully_known_company_qualifies_cleanly(make_job, settings, now):
    fresh = _fresh(
        [make_job(company="Ideal Co", title="Plant Manager", days_old=1,
                  employees="51 to 200", industry="Manufacturing",
                  description="Rapidly growing, new facility opening."),
         make_job(company="Ideal Co", title="Welder", days_old=2,
                  employees="51 to 200", industry="Manufacturing"),
         make_job(company="Ideal Co", title="Machinist", days_old=3,
                  employees="51 to 200", industry="Manufacturing")],
        settings, now,
    )[0]
    result = score_company(fresh, settings=settings)
    assert result.status == QualificationStatus.QUALIFIED.value
    assert result.review_flags == []
    assert result.breakdown["size_bonus"] == 15.0
    assert result.breakdown["industry_bonus"] == 12.0


def test_unknown_size_and_industry_cannot_silently_qualify(make_job, settings, now):
    """Previously a single job + one growth word scored 53 and passed."""
    fresh = _fresh(
        [make_job(company="Mystery Co", title="Random Role", days_old=1,
                  description="expansion")],
        settings, now,
    )[0]
    result = score_company(fresh, settings=settings)
    assert result.status != QualificationStatus.QUALIFIED.value


def test_low_applicant_count_boosts_score(make_job, settings, now):
    base = _fresh([make_job(company="A Co", employees="51 to 200",
                            industry="Manufacturing")], settings, now)[0]
    boosted = _fresh([make_job(company="B Co", employees="51 to 200",
                               industry="Manufacturing", applicant_count=5)],
                     settings, now)[0]
    assert score_company(boosted, settings=settings).score > score_company(base, settings=settings).score


def test_missing_applicant_count_never_disqualifies(make_job, settings, now):
    fresh = _fresh([make_job(company="No Signal Co", employees="51 to 200",
                             industry="Manufacturing", days_old=1)], settings, now)[0]
    result = score_company(fresh, settings=settings)
    assert result.breakdown["applicant_signal"] == "UNAVAILABLE"
    assert "APPLICANT" not in " ".join(result.reasons)


# ---------------------------------------------------------------------------
# Search intent must never masquerade as an observed company fact.
# A live run labelled Molson Coors, Johnsonville and Merck as
# "Warehousing & Distribution" purely because that was the query's industry.
# ---------------------------------------------------------------------------
def test_search_industry_is_not_treated_as_evidence(make_job, settings, now):
    job = make_job(company="Molson Coors", title="Brewery Operator", industry=None)
    job.search_industry = "Warehousing & Distribution"
    fresh = _fresh([job], settings, now)[0]

    profile = build_profile(fresh)
    assert profile.industry_known is False, "a query is not evidence"
    assert profile.industry_source == "DISCOVERY_INTENT"
    assert profile.client_industry == "Warehousing & Distribution"  # kept as a hint


def test_unverified_industry_earns_no_bonus_and_flags_review(make_job, settings, now):
    # A role the occupation taxonomy cannot place, so the only industry signal
    # available is the query's own intent.
    job = make_job(company="Mystery Foods", title="Zorb Wrangler",
                   employees="51 to 200", industry=None, days_old=1)
    job.search_industry = "Warehousing & Distribution"
    fresh = _fresh([job], settings, now)[0]

    result = score_company(fresh, settings=settings)
    assert result.breakdown["industry_bonus"] == 0.0
    assert "INDUSTRY_UNKNOWN" in result.review_flags
    assert result.status == QualificationStatus.NEEDS_REVIEW.value


def test_observed_industry_still_counts(make_job, settings, now):
    job = make_job(company="Real Brewery", title="Brewer", employees="51 to 200",
                   industry="Breweries", days_old=1,
                   sector="Food & Beverage Manufacturing")
    fresh = _fresh([job], settings, now)[0]

    profile = build_profile(fresh)
    assert profile.industry_known is True
    assert profile.client_industry == "Food & Beverage Manufacturing"
    assert profile.industry_source == "JOB_BOARD"
    assert score_company(fresh, settings=settings).breakdown["industry_bonus"] == 12.0


def test_an_observed_industry_from_another_sector_is_reviewed_with_its_reason(
        make_job, settings, now):
    """The Molson Coors case: a brewery surfaced by a warehousing query."""
    job = make_job(company="Real Brewery", title="Brewer", employees="51 to 200",
                   industry="Breweries", sector="Warehousing & Distribution")
    result = score_company(_fresh([job], settings, now)[0], settings=settings)
    assert result.status == QualificationStatus.NEEDS_REVIEW.value
    assert result.review_flags == ["INDUSTRY_MISMATCH"]
    assert result.breakdown["selected_sector"] == "Warehousing & Distribution"
    assert result.breakdown["industry_bonus"] == 0.0


# ---------------------------------------------------------------------------
# Phase 5 rules
# ---------------------------------------------------------------------------
def _known(make_job, settings, now, **overrides):
    """A company that passes every rule unless ``overrides`` breaks one."""
    fields = dict(company="Ideal Co", title="Welder", employees="51 to 200",
                  industry="Manufacturing", days_old=1)
    fields.update(overrides)
    return score_company(_fresh([make_job(**fields)], settings, now)[0], settings=settings)


@pytest.mark.parametrize("employees,status,code", [
    ("1 to 10", "REJECTED", "EMPLOYEE_SIZE_BELOW_MIN"),
    ("fewer than 10", "REJECTED", "EMPLOYEE_SIZE_BELOW_MIN"),
    ("11 to 50", "QUALIFIED", None),
    ("200", "QUALIFIED", None),
    ("201 to 500", "REJECTED", "EMPLOYEE_SIZE_ABOVE_MAX"),
    ("10,000+", "REJECTED", "EMPLOYEE_SIZE_ABOVE_MAX"),
    ("1 to 50", "NEEDS_REVIEW", "EMPLOYEE_SIZE_SPANS_LIMIT"),
    ("more than 100", "NEEDS_REVIEW", "EMPLOYEE_SIZE_SPANS_LIMIT"),
    (None, "NEEDS_REVIEW", "EMPLOYEE_SIZE_UNKNOWN"),
])
def test_size_rule_boundaries(make_job, settings, now, employees, status, code):
    result = _known(make_job, settings, now, employees=employees)
    assert result.status == status
    if code:
        assert code in result.reasons + result.review_flags


def test_unknown_size_is_reviewed_whatever_the_score(make_job, settings, now):
    result = _known(make_job, settings, now, employees=None, industry=None,
                    title="Zorb Wrangler", days_old=13)
    assert result.status == QualificationStatus.NEEDS_REVIEW.value
    assert result.reasons == []


def test_the_score_ranks_but_never_decides(make_job, settings, now):
    """A company passing every rule qualifies however weak its hiring signals."""
    weak = _known(make_job, settings, now, days_old=13)
    strong = _known(make_job, settings, now, days_old=1, applicant_count=5,
                    description="Rapidly growing, new facility, expansion.")
    assert weak.status == strong.status == QualificationStatus.QUALIFIED.value
    assert weak.score < strong.score
    assert not hasattr(settings, "qualify_threshold")


@pytest.mark.parametrize("roles,senior,reject_at,review_at,verdict", [
    (4, False, 4, 2, "MATURE"),
    (3, False, 4, 2, "POSSIBLE"),
    (2, True, 4, 2, "MATURE"),       # a senior TA leader plus the review threshold
    (1, True, 4, 2, "NONE"),
    (4, False, 6, 3, "POSSIBLE"),    # the configuration decides, nothing is hardcoded
    (2, True, 6, 3, "NONE"),
    (6, False, 6, 3, "MATURE"),
])
def test_internal_ta_uses_only_the_configured_thresholds(settings, roles, senior,
                                                         reject_at, review_at, verdict):
    settings.internal_ta_reject_threshold = reject_at
    settings.internal_ta_review_threshold = review_at
    signal = InternalTASignal(ta_role_count=roles, has_senior_ta_leader=senior)
    assert evaluate_internal_ta(signal, settings)[0] == verdict


@pytest.mark.parametrize("sector,industry,status,code", [
    ("Manufacturing", "Motor Vehicle Manufacturing", "QUALIFIED", None),
    # The taxonomy keeps these apart: a neighbouring sector is reviewed.
    ("Manufacturing", "Plastics", "NEEDS_REVIEW", "INDUSTRY_MISMATCH"),
    ("Manufacturing", "Industrial Machinery Manufacturing", "NEEDS_REVIEW", "INDUSTRY_MISMATCH"),
    ("Logistics & Transportation", "Warehousing", "NEEDS_REVIEW", "INDUSTRY_MISMATCH"),
    ("Manufacturing", "Automotive Dealers", "NEEDS_REVIEW", "INDUSTRY_MISMATCH"),
    ("Retail", "Automotive Dealers", "QUALIFIED", None),
    ("Retail", "Building Materials", "QUALIFIED", None),
    ("Hospitality/Hotels", "Restaurants & Food Service", "NEEDS_REVIEW", "INDUSTRY_UNCLASSIFIED"),
    ("Construction", "Hospitals and Health Care", "NEEDS_REVIEW", "INDUSTRY_UNCLASSIFIED"),
    ("Construction", "Hotels & Motels", "NEEDS_REVIEW", "INDUSTRY_MISMATCH"),
    (None, "Manufacturing", "NEEDS_REVIEW", "SECTOR_NOT_SELECTED"),
])
def test_industry_relevance_to_the_selected_sector(make_job, settings, now,
                                                   sector, industry, status, code):
    result = _known(make_job, settings, now, sector=sector, industry=industry)
    assert result.status == status
    if code:
        assert code in result.reasons + result.review_flags


def test_job_description_words_are_not_industry_evidence(make_job, settings, now):
    """Keywords in a posting ("manufacturing floor") say nothing reliable about the employer."""
    result = _known(make_job, settings, now, industry=None, title="Zorb Wrangler",
                    description="Join our manufacturing floor and hotel catering team.")
    assert result.breakdown["industry_source"] != "JOB_DESCRIPTION"
    assert result.breakdown["industry_verdict"] == "REVIEW"
    assert "INDUSTRY_UNKNOWN" in result.review_flags


def test_every_rejection_reason_is_reported(make_job, settings, now):
    result = _known(make_job, settings, now, company="Bolt Staffing Group",
                    employees="5,000 to 10,000", industry="Hotels & Motels")
    assert result.status == QualificationStatus.REJECTED.value
    assert result.reasons == ["STAFFING_AGENCY", "EMPLOYEE_SIZE_ABOVE_MAX"]
    assert result.review_flags == ["INDUSTRY_MISMATCH"], "recorded alongside"


@pytest.mark.parametrize("name", ["Acme Corporation", "Superior Consulting Group",
                                  "Staffordshire Castings", "Apex Recruitment Drive Parts"])
def test_agency_words_must_be_whole_words(name):
    expected = name == "Apex Recruitment Drive Parts"
    assert detect_agency(name, "")[0] is expected, name


def test_ta_roles_must_be_whole_words():
    assert detect_internal_ta(["Seasonal Christmas Associate"]).ta_role_count == 0
    assert detect_internal_ta(["Technical Recruiters"]).ta_role_count == 1


@pytest.mark.parametrize("count,bonus", [(5, 8.0), (20, 8.0), (45, 3.0), (400, 0.0)])
def test_linkedin_applicants_only_ever_add_points(make_job, settings, now, count, bonus):
    result = _known(make_job, settings, now, applicant_count=count)
    assert result.breakdown["applicant_bonus"] == bonus
    assert result.status == QualificationStatus.QUALIFIED.value, "never a rejection"


def test_growth_words_must_be_whole_words(make_job, settings, now):
    result = _known(make_job, settings, now, description="Descaling and regrowth of coral.")
    assert result.breakdown["growth_signals"] == []
