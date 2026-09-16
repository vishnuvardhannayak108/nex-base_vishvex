"""Module 6: Qualification Gate.

Scores each company on direct-employer status, employee size, industry, job
freshness, multiple openings, growth signals, applicant-count signal and
persistent hiring. Rejections are always paired with a clear reason. Only
evidence actually observed is used - nothing is invented.

Three outcomes: ``QUALIFIED``, ``REJECTED``, and ``NEEDS_REVIEW`` for companies
whose evidence is incomplete (e.g. unknown employee size).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from nexbase.config import Settings, get_settings
from nexbase.core.enums import QualificationStatus
from nexbase.logging_setup import get_logger
from nexbase.pipeline.freshness import FreshCompany
from nexbase.pipeline.profile import CompanyProfile, build_profile

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
    "temp agency", "temporary staffing", "consulting group", "rpo",
)

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

GROWTH_SIGNAL_KEYWORDS = (
    "newly created", "newly-created", "rapidly growing", "fast growing",
    "fast-growing", "expanding", "expansion", "growth", "scaling",
    "new facility", "new plant", "new location", "new site", "new branch",
    "second shift", "third shift", "additional shift", "increased demand",
    "record demand", "new contract", "new line", "capacity increase",
    "ramping up", "doubling", "restructuring", "operational change",
    "process improvement", "transformation", "modernization", "automation rollout",
)


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
    """Return ``(is_intermediary, evidence)``."""
    name = (company_name or "").lower()
    for keyword in AGENCY_NAME_KEYWORDS:
        if keyword in name:
            return True, f"NAME:{keyword}"
    text = descriptions.lower()
    for phrase in AGENCY_SELF_DESCRIPTION:
        if phrase in text:
            return True, f"SELF_DESCRIPTION:{phrase}"
    return False, None


def evaluate_size(
    profile: CompanyProfile, settings: Settings
) -> tuple[str, str | None]:
    """Return ``(verdict, reason)`` where verdict is IN_RANGE/OVERSIZE/UNDERSIZE/UNKNOWN."""
    if not profile.size_known:
        return "UNKNOWN", None

    lo = profile.employee_size_min
    hi = profile.employee_size_max

    # Wholly above the ceiling.
    if lo is not None and lo > settings.size_filter_max:
        return "OVERSIZE", "EMPLOYEE_SIZE_ABOVE_MAX"
    # Wholly below the floor.
    if hi is not None and hi < settings.size_filter_min:
        return "UNDERSIZE", "EMPLOYEE_SIZE_BELOW_MIN"
    # Open-ended upper bound starting inside the band, e.g. "10,000+".
    if hi is None and lo is not None and lo > settings.size_filter_max:
        return "OVERSIZE", "EMPLOYEE_SIZE_ABOVE_MAX"
    return "IN_RANGE", None


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
    text = " ".join((job.description or "") for job, _ in fresh.fresh_jobs).lower()
    hits = [k for k in GROWTH_SIGNAL_KEYWORDS if k in text]
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
    """Evaluate a single fresh company against the qualification criteria."""
    settings = settings or get_settings()
    profile = profile or build_profile(fresh)

    name = fresh.company.company_name_normalized
    descriptions = " ".join((job.description or "") for job, _ in fresh.fresh_jobs)

    reasons: list[str] = []
    review_flags: list[str] = []
    breakdown: dict = {}

    # --- Hard rejection 1: intermediary -----------------------------------
    is_agency, agency_evidence = detect_agency(fresh.company.company_name, descriptions)
    breakdown["is_staffing_agency"] = is_agency
    breakdown["agency_evidence"] = agency_evidence
    if is_agency:
        return QualificationResult(
            company_name=name,
            status=QualificationStatus.REJECTED.value,
            score=0.0,
            reasons=["STAFFING_AGENCY"],
            breakdown=breakdown,
            profile=profile,
        )

    # --- Employee size ----------------------------------------------------
    size_verdict, size_reason = evaluate_size(profile, settings)
    breakdown["size_known"] = profile.size_known
    breakdown["size_verdict"] = size_verdict
    breakdown["employee_size_min"] = profile.employee_size_min
    breakdown["employee_size_max"] = profile.employee_size_max
    breakdown["size_source"] = profile.size_source

    if size_verdict in ("UNDERSIZE", "OVERSIZE"):
        return QualificationResult(
            company_name=name,
            status=QualificationStatus.REJECTED.value,
            score=0.0,
            reasons=[size_reason],
            breakdown=breakdown,
            profile=profile,
        )

    if size_verdict == "UNKNOWN":
        review_flags.append("EMPLOYEE_SIZE_UNKNOWN")

    # --- Industry ---------------------------------------------------------
    breakdown["industry_known"] = profile.industry_known
    breakdown["client_industry"] = profile.client_industry
    breakdown["industry_source"] = profile.industry_source
    breakdown["industry_state"] = profile.industry_state
    if not profile.industry_known:
        review_flags.append("INDUSTRY_UNKNOWN")

    # --- Internal TA filter ----------------------------------------------
    ta = profile.internal_ta
    breakdown["internal_ta_roles"] = ta.ta_role_count
    breakdown["internal_ta_senior_leader"] = ta.has_senior_ta_leader
    if ta.ta_role_count >= settings.internal_ta_reject_threshold or ta.is_mature:
        return QualificationResult(
            company_name=name,
            status=QualificationStatus.REJECTED.value,
            score=0.0,
            reasons=["MATURE_INTERNAL_TA"],
            breakdown=breakdown,
            profile=profile,
        )
    if ta.ta_role_count >= settings.internal_ta_review_threshold:
        review_flags.append("POSSIBLE_INTERNAL_TA")

    # --- Positive scoring -------------------------------------------------
    score = 0.0

    breakdown["direct_employer"] = 25.0
    score += 25.0

    if profile.size_known and size_verdict == "IN_RANGE":
        breakdown["size_bonus"] = 15.0
        score += 15.0
    elif profile.size_known:
        breakdown["size_bonus"] = 0.0
    else:
        breakdown["size_bonus"] = 0.0

    # Only a *verified* industry earns points. An OCCUPATION_TAXONOMY_HINT or
    # DISCOVERY_INTENT hint is something we guessed, not evidence about the
    # employer, so it scores nothing and leaves INDUSTRY_UNKNOWN standing.
    if profile.industry_known and profile.client_industry:
        breakdown["industry_bonus"] = 12.0
        score += 12.0
    else:
        breakdown["industry_bonus"] = 0.0

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

    score += (
        breakdown["freshness_bonus"]
        + breakdown["intensity_bonus"]
        + growth
        + applicants
        + breakdown["persistence_bonus"]
    )

    breakdown["best_freshness_priority"] = fresh.best_priority
    breakdown["total"] = round(score, 2)

    # --- Verdict ----------------------------------------------------------
    if score < settings.qualify_threshold:
        reasons.append("LOW_SCORE")
        status = QualificationStatus.REJECTED
    elif review_flags:
        status = QualificationStatus.NEEDS_REVIEW
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
