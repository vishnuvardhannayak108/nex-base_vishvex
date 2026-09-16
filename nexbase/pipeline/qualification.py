"""Qualification Engine: the gate before any paid enrichment.

Rules (Master Plan, Phase 5):

- **Size**: <11 reject, 11-200 eligible, >200 reject, unknown -> NEEDS_REVIEW.
  A range that crosses a limit ("1 to 50") is not known to be eligible, so it
  is reviewed too.
- **Agency exclusion**: staffing, recruitment, executive search, intermediaries.
- **Internal TA filter**: a mature in-house TA team rejects, a smaller one is
  reviewed.
- **Industry relevance** against the run's selected sector. A mismatch is
  reviewed, not rejected: the industry comes from a keyword classifier that
  mislabels real sector members (a "Restaurants & Food Service" label reads as
  food manufacturing).
- **Hiring signals** scoring, including the LinkedIn applicant signal (<= 20 is
  positive, never a rejection).

Every rejection reason is collected, not just the first. A company with any
review flag is NEEDS_REVIEW rather than rejected on a score its missing evidence
held down. Only evidence actually observed is used - nothing is invented.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from nexbase.config import Settings, get_settings
from nexbase.core.enums import ClientIndustry, QualificationStatus
from nexbase.discovery import taxonomy
from nexbase.logging_setup import get_logger
from nexbase.pipeline.company_identity import FreshCompany
from nexbase.pipeline.profile import OBSERVED, CompanyProfile, build_profile, phrase_pattern

# ---------------------------------------------------------------------------
# Intermediary detection (brief: exclude staffing, recruitment, exec search)
# ---------------------------------------------------------------------------

#: Matched against the company NAME only. Matching descriptions produced false
#: rejections - a manufacturer whose JD says "placement of components" is not
#: a staffing agency.
AGENCY_NAME_KEYWORDS = (
    "staffing", "staff solutions", "recruiting", "recruitment", "recruiters",
    "headhunter", "head hunter", "talent agency", "talent solutions",
    "talent partners", "executive search", "search group", "search partners",
    "search firm", "staff augmentation", "contingent workforce",
    "employment agency", "personnel services", "manpower", "workforce solutions",
    "placement services", "placement agency", "hr solutions", "peo services",
    "temp agency", "temporary staffing", "rpo",
)
_AGENCY_NAME_RE = phrase_pattern(AGENCY_NAME_KEYWORDS)

#: Matched against descriptions, but only as *self-description* phrases that a
#: direct employer would not write about itself.
AGENCY_SELF_DESCRIPTION = (
    "we are a staffing agency",
    "we are a recruiting firm",
    "our client is seeking",
    "our client is looking",
    "on behalf of our client",
    "we are recruiting on behalf",
    "staffing agency specializing",
    "recruitment agency specializing",
    "executive search firm",
    "we place candidates",
    "our staffing team",
    "client company is seeking",
)
_AGENCY_SELF_RE = phrase_pattern(AGENCY_SELF_DESCRIPTION)

GROWTH_SIGNAL_KEYWORDS = (
    "newly created", "newly-created", "rapidly growing", "fast growing",
    "fast-growing", "expanding", "expansion", "growth", "scaling",
    "new facility", "new plant", "new location", "new site", "new branch",
    "second shift", "third shift", "additional shift", "increased demand",
    "record demand", "new contract", "new line", "capacity increase",
    "ramping up", "doubling", "restructuring", "operational change",
    "process improvement", "transformation", "modernization", "automation rollout",
)
_GROWTH_RE = phrase_pattern(GROWTH_SIGNAL_KEYWORDS)


@dataclass
class QualificationResult:
    company_name: str
    status: str
    score: float
    reasons: list[str] = field(default_factory=list)
    review_flags: list[str] = field(default_factory=list)
    breakdown: dict = field(default_factory=dict)
    profile: CompanyProfile | None = None

    @property
    def qualified(self) -> bool:
        return self.status == QualificationStatus.QUALIFIED.value

    @property
    def needs_review(self) -> bool:
        return self.status == QualificationStatus.NEEDS_REVIEW.value


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def detect_agency(company_name: str | None, descriptions: str) -> tuple[bool, str | None]:
    """Return ``(is_intermediary, evidence)``.

    Whole words only: "rpo" is an agency, "Acme Corporation" is not.
    """
    match = _AGENCY_NAME_RE.search(company_name or "")
    if match:
        return True, f"NAME:{match.group(0).lower()}"
    match = _AGENCY_SELF_RE.search(descriptions or "")
    if match:
        return True, f"SELF_DESCRIPTION:{match.group(0).lower()}"
    return False, None


def evaluate_size(
    profile: CompanyProfile, settings: Settings
) -> tuple[str, str | None]:
    """Return ``(verdict, reason)``.

    Verdicts: IN_RANGE, OVERSIZE, UNDERSIZE (both rejections), UNKNOWN, and
    SPANS_LIMIT for a range only partly inside the band - "1 to 50" may be
    under 11, "more than 100" may be over 200. Both of those go to review.
    """
    if not profile.size_known:
        return "UNKNOWN", "EMPLOYEE_SIZE_UNKNOWN"

    lo = profile.employee_size_min
    hi = profile.employee_size_max

    # Wholly above the ceiling ("201 to 500", "10,000+").
    if lo is not None and lo > settings.size_filter_max:
        return "OVERSIZE", "EMPLOYEE_SIZE_ABOVE_MAX"
    # Wholly below the floor ("1 to 10", "fewer than 10").
    if hi is not None and hi < settings.size_filter_min:
        return "UNDERSIZE", "EMPLOYEE_SIZE_BELOW_MIN"
    if (lo is not None and lo >= settings.size_filter_min
            and hi is not None and hi <= settings.size_filter_max):
        return "IN_RANGE", None
    return "SPANS_LIMIT", "EMPLOYEE_SIZE_SPANS_LIMIT"


# ---------------------------------------------------------------------------
# Industry relevance against the selected sector
# ---------------------------------------------------------------------------
#: NAICS publishes these two-digit codes as one sector (31-33, 44-45, 48-49).
_NAICS_SECTOR_OF = {"32": "31", "33": "31", "45": "44", "49": "48"}


@lru_cache(maxsize=None)
def naics_sectors(client_sector: str) -> frozenset[str]:
    """NAICS sectors a NexBase sector's industries sit in, from the BLS data."""
    return frozenset(
        _NAICS_SECTOR_OF.get(industry.naics3[:2], industry.naics3[:2])
        for industry in taxonomy.load_industries()
        if industry.client_industry == client_sector)


def evaluate_industry(profile: CompanyProfile, sector: str | None) -> tuple[str, str | None]:
    """Return ``(verdict, reason)``: RELEVANT or REVIEW.

    Only an industry observed about the employer can make a company relevant.
    Anything else is reviewed with the reason.
    """
    if not sector:
        return "REVIEW", "SECTOR_NOT_SELECTED"
    if profile.industry_state != OBSERVED:
        return "REVIEW", "INDUSTRY_UNKNOWN"
    industry = profile.client_industry
    if industry is None:
        return "REVIEW", "INDUSTRY_UNCLASSIFIED"
    if industry == sector:
        return "RELEVANT", None
    if naics_sectors(industry) & naics_sectors(sector):
        # Manufacturing is all of NAICS 31-33, so a plastics or food
        # manufacturer is a manufacturer. The reverse is not true.
        if sector == ClientIndustry.MANUFACTURING.value:
            return "RELEVANT", None
        return "REVIEW", "INDUSTRY_ADJACENT_SECTOR"
    return "REVIEW", "INDUSTRY_MISMATCH"


def freshness_bonus(fresh: FreshCompany) -> float:
    min_age = fresh.min_age_days
    if min_age is None:
        return 0.0
    if min_age <= 2:
        return 15.0
    if min_age <= 7:
        return 10.0
    if min_age <= 12:
        return 6.0
    return 3.0


def intensity_bonus(fresh: FreshCompany) -> float:
    """Multiple openings are a *supporting* signal, so this is capped modestly."""
    n = fresh.hiring_intensity
    if n >= 5:
        return 12.0
    if n >= 3:
        return 9.0
    if n == 2:
        return 6.0
    return 3.0


def growth_bonus(fresh: FreshCompany) -> tuple[float, list[str]]:
    text = " ".join((job.description or "") for job, _ in fresh.fresh_jobs)
    hits = list(dict.fromkeys(m.group(0).lower() for m in _GROWTH_RE.finditer(text)))
    if len(hits) >= 3:
        return 10.0, hits[:5]
    if len(hits) == 2:
        return 7.0, hits
    if len(hits) == 1:
        return 4.0, hits
    return 0.0, []


def applicant_bonus(fresh: FreshCompany, settings: Settings) -> tuple[float, str | None]:
    """Brief: prioritise LinkedIn jobs with <= 20 applicants, when available."""
    count = fresh.company.min_applicant_count
    if count is None:
        return 0.0, "UNAVAILABLE"  # never a disqualification
    if count <= settings.linkedin_max_applicants:
        return 8.0, f"LOW_APPLICANTS:{count}"
    if count <= settings.linkedin_max_applicants * 3:
        return 3.0, f"MODERATE_APPLICANTS:{count}"
    return 0.0, f"HIGH_APPLICANTS:{count}"


def persistence_bonus(profile: CompanyProfile) -> float:
    """Brief: persistent hiring needs score higher. Requires cross-run history."""
    runs = profile.persistent_hiring_runs
    if runs >= 4:
        return 8.0
    if runs >= 2:
        return 5.0
    return 0.0


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_company(
    fresh: FreshCompany,
    profile: CompanyProfile | None = None,
    settings: Settings | None = None,
) -> QualificationResult:
    """Evaluate a single fresh company against every qualification rule."""
    settings = settings or get_settings()
    profile = profile or build_profile(fresh)

    name = fresh.company.company_name_normalized
    descriptions = " ".join((job.description or "") for job, _ in fresh.fresh_jobs)

    reasons: list[str] = []
    review_flags: list[str] = []
    breakdown: dict = {}

    # --- Agency exclusion -------------------------------------------------
    is_agency, agency_evidence = detect_agency(fresh.company.company_name, descriptions)
    breakdown["is_staffing_agency"] = is_agency
    breakdown["agency_evidence"] = agency_evidence
    if is_agency:
        reasons.append("STAFFING_AGENCY")

    # --- Employee size ----------------------------------------------------
    size_verdict, size_reason = evaluate_size(profile, settings)
    breakdown["size_known"] = profile.size_known
    breakdown["size_verdict"] = size_verdict
    breakdown["employee_size_min"] = profile.employee_size_min
    breakdown["employee_size_max"] = profile.employee_size_max
    breakdown["size_source"] = profile.size_source
    if size_verdict in ("UNDERSIZE", "OVERSIZE"):
        reasons.append(size_reason)
    elif size_reason:
        review_flags.append(size_reason)

    # --- Industry relevance to the selected sector ------------------------
    sector = fresh.company.search_industry
    industry_verdict, industry_reason = evaluate_industry(profile, sector)
    breakdown["selected_sector"] = sector
    breakdown["industry_known"] = profile.industry_known
    breakdown["client_industry"] = profile.client_industry
    breakdown["industry_source"] = profile.industry_source
    breakdown["industry_state"] = profile.industry_state
    breakdown["industry_verdict"] = industry_verdict
    if industry_reason:
        review_flags.append(industry_reason)

    # --- Internal TA filter ----------------------------------------------
    ta = profile.internal_ta
    breakdown["internal_ta_roles"] = ta.ta_role_count
    breakdown["internal_ta_senior_leader"] = ta.has_senior_ta_leader
    if ta.ta_role_count >= settings.internal_ta_reject_threshold or ta.is_mature:
        reasons.append("MATURE_INTERNAL_TA")
    elif ta.ta_role_count >= settings.internal_ta_review_threshold:
        review_flags.append("POSSIBLE_INTERNAL_TA")

    # --- Company identity -------------------------------------------------
    breakdown["identity_basis"] = fresh.company.identity_basis
    if fresh.company.identity_ambiguous:
        review_flags.append("AMBIGUOUS_COMPANY_IDENTITY")

    # --- Score: rule outcomes plus hiring signals -------------------------
    breakdown["direct_employer"] = 0.0 if is_agency else 25.0
    breakdown["size_bonus"] = 15.0 if size_verdict == "IN_RANGE" else 0.0
    # Only an industry observed to match the sector earns points. A hint
    # (OCCUPATION_TAXONOMY_HINT, DISCOVERY_INTENT) is not evidence.
    breakdown["industry_bonus"] = 12.0 if industry_verdict == "RELEVANT" else 0.0
    breakdown["freshness_bonus"] = freshness_bonus(fresh)
    breakdown["intensity_bonus"] = intensity_bonus(fresh)
    growth, growth_hits = growth_bonus(fresh)
    breakdown["growth_bonus"] = growth
    breakdown["growth_signals"] = growth_hits
    applicants, applicant_note = applicant_bonus(fresh, settings)
    breakdown["applicant_bonus"] = applicants
    breakdown["applicant_signal"] = applicant_note
    breakdown["persistence_bonus"] = persistence_bonus(profile)
    breakdown["persistent_hiring_runs"] = profile.persistent_hiring_runs
    breakdown["freshest_job_age_days"] = fresh.min_age_days

    score = sum(breakdown[k] for k in (
        "direct_employer", "size_bonus", "industry_bonus", "freshness_bonus",
        "intensity_bonus", "growth_bonus", "applicant_bonus", "persistence_bonus"))
    breakdown["total"] = round(score, 2)

    # --- Verdict ----------------------------------------------------------
    # A rule failure rejects. Missing or ambiguous evidence is reviewed, never
    # rejected on a score that the missing evidence held down.
    if reasons:
        status = QualificationStatus.REJECTED
    elif review_flags:
        status = QualificationStatus.NEEDS_REVIEW
    elif score < settings.qualify_threshold:
        reasons.append("LOW_SCORE")
        status = QualificationStatus.REJECTED
    else:
        status = QualificationStatus.QUALIFIED

    return QualificationResult(
        company_name=name,
        status=status.value,
        score=round(score, 2),
        reasons=reasons,
        review_flags=review_flags,
        breakdown=breakdown,
        profile=profile,
    )


class QualificationGate:
    """Qualifies fresh companies and logs every decision with reasons."""

    def __init__(self, settings: Settings | None = None, logger=None) -> None:
        self.settings = settings or get_settings()
        self.log = logger or get_logger("nexbase.pipeline.qualification")

    def qualify(
        self,
        companies: list[FreshCompany],
        profiles: dict[tuple[str, str], CompanyProfile] | None = None,
    ) -> list[QualificationResult]:
        profiles = profiles or {}
        results: list[QualificationResult] = []
        for company in companies:
            key = company.company.dedup_key
            result = score_company(company, profiles.get(key), self.settings)
            event = (
                "qualification_pass"
                if result.qualified
                else "qualification_review"
                if result.needs_review
                else "qualification_reject"
            )
            self.log.info(
                event,
                company=result.company_name,
                score=result.score,
                status=result.status,
                reasons=result.reasons,
                review_flags=result.review_flags,
            )
            results.append(result)
        return results
