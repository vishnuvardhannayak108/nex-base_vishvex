"""Qualification: size rule, industry, intermediaries, internal TA, review flags."""
from __future__ import annotations

import pytest

from nexbase.core.enums import QualificationStatus
from nexbase.pipeline.company_identity import prepare_companies
from nexbase.pipeline.profile import (
    build_profile,
    classify_industry,
    detect_internal_ta,
    parse_employee_range,
)
from nexbase.pipeline.qualification import detect_agency, evaluate_size, score_company


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
    "text,expected",
    [
        ("Industrial Manufacturing", "Manufacturing"),
        ("General Contractor", "Construction"),
        ("Hotels & Motels", "Hospitality/Hotels"),
        ("Commercial Property Management", "Property Management/Real Estate"),
        ("Trucking & Freight", "Logistics & Transportation"),
        ("Food Production", "Food & Beverage Manufacturing"),
        ("Injection Molding", "Plastics/Rubber"),
        ("Fulfillment Center", "Warehousing & Distribution"),
        ("Investment Banking", None),
    ],
)
def test_classify_industry_covers_all_ten_brief_industries(text, expected):
    assert classify_industry(text) == expected


def test_hospitality_and_real_estate_are_no_longer_missing():
    """Both were absent from the old keyword list."""
    assert classify_industry("Hotel & Resort Operations") == "Hospitality/Hotels"
    assert classify_industry("Real Estate Leasing") == "Property Management/Real Estate"


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
    assert signal.is_mature is False


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
    job = make_job(company="Real Brewery", title="Brewer",
                   employees="51 to 200", industry="Food And Beverages", days_old=1)
    job.search_industry = "Warehousing & Distribution"
    fresh = _fresh([job], settings, now)[0]

    profile = build_profile(fresh)
    assert profile.industry_known is True
    assert profile.client_industry == "Food & Beverage Manufacturing"
    assert profile.industry_source == "JOB_BOARD"
    assert score_company(fresh, settings=settings).breakdown["industry_bonus"] == 12.0
