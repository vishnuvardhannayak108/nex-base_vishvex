"""Company profile assembly from freely observed evidence.

The qualification gate needs an employee range and an industry. Previously
neither was ever supplied, so the configured size rule and the industry mix were
dead code. This module builds a :class:`CompanyProfile` from facts already
captured during discovery - Indeed's ``company_num_employees`` and
``company_industry`` columns, the SOC/O*NET-derived industry of the roles being
advertised, and the job descriptions themselves.

Everything is evidence-backed. When a fact cannot be observed, the
corresponding ``*_known`` flag stays ``False`` and the gate treats it as
unknown rather than assuming a value.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from nexbase.discovery import taxonomy
from nexbase.pipeline.company_identity import FreshCompany

# ---------------------------------------------------------------------------
# Employee size parsing
# ---------------------------------------------------------------------------

_NUM = r"([\d][\d,\.]*)\s*([kKmM]?)"
_RANGE_RE = re.compile(rf"{_NUM}\s*(?:to|-|–|—|until)\s*{_NUM}")
_PLUS_RE = re.compile(rf"{_NUM}\s*\+")
_MORE_THAN_RE = re.compile(rf"(?:more than|over|above|\>)\s*{_NUM}", re.IGNORECASE)
_FEWER_THAN_RE = re.compile(rf"(?:fewer than|less than|under|below|\<)\s*{_NUM}", re.IGNORECASE)
_SINGLE_RE = re.compile(rf"^\s*{_NUM}\s*(?:employees?|people|staff|workers?)?\s*$", re.IGNORECASE)


def _scale(number: str, suffix: str) -> int | None:
    try:
        value = float(number.replace(",", ""))
    except ValueError:
        return None
    if suffix.lower() == "k":
        value *= 1_000
    elif suffix.lower() == "m":
        value *= 1_000_000
    return int(value)


def parse_employee_range(text: str | None) -> tuple[int | None, int | None]:
    """Parse an employee-count string into ``(min, max)``.

    Handles the shapes job boards actually emit: ``"51 to 200"``,
    ``"1,001 to 5,000"``, ``"10,000+"``, ``"more than 500"``,
    ``"fewer than 25"``, ``"200"``. Returns ``(None, None)`` when unparseable -
    never a guess.
    """
    if not text:
        return None, None
    cleaned = str(text).strip().lower().replace("employees", " ").strip()
    if not cleaned:
        return None, None

    match = _RANGE_RE.search(cleaned)
    if match:
        lo = _scale(match.group(1), match.group(2))
        hi = _scale(match.group(3), match.group(4))
        if lo is not None and hi is not None and lo > hi:
            lo, hi = hi, lo
        return lo, hi

    match = _MORE_THAN_RE.search(cleaned)
    if match:
        return _scale(match.group(1), match.group(2)), None

    match = _FEWER_THAN_RE.search(cleaned)
    if match:
        return None, _scale(match.group(1), match.group(2))

    match = _PLUS_RE.search(cleaned)
    if match:
        return _scale(match.group(1), match.group(2)), None

    match = _SINGLE_RE.match(cleaned)
    if match:
        value = _scale(match.group(1), match.group(2))
        return value, value

    return None, None


# ---------------------------------------------------------------------------
# Industry classification
# ---------------------------------------------------------------------------

#: Free-text industry label -> the client's ten canonical industries.
_INDUSTRY_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Food & Beverage Manufacturing", ("food", "beverage", "brewery", "dairy", "bakery", "meat process", "food product")),
    ("Plastics/Rubber", ("plastic", "rubber", "polymer", "injection mold", "resin", "composites")),
    ("Industrial Equipment/Machinery", ("machinery", "industrial equipment", "machine shop", "cnc", "capital equipment", "heavy equipment", "tooling")),
    ("Warehousing & Distribution", ("warehous", "distribution center", "fulfillment", "3pl", "third-party logistics", "storage")),
    ("Logistics & Transportation", ("logistic", "transport", "trucking", "freight", "shipping", "carrier", "courier", "delivery", "supply chain", "fleet")),
    ("Construction", ("construction", "contractor", "building", "civil engineering", "roofing", "hvac", "plumbing", "electrical contract", "concrete", "excavat", "general contract")),
    ("Hospitality/Hotels", ("hospitality", "hotel", "resort", "restaurant", "food service", "catering", "lodging", "casino", "hotels & motels")),
    ("Property Management/Real Estate", ("real estate", "property management", "realty", "leasing", "facilities management", "commercial property")),
    ("Retail", ("retail", "e-commerce", "ecommerce", "store", "merchandis", "consumer goods", "grocery", "apparel")),
    ("Manufacturing", ("manufactur", "industrial", "fabricat", "metal", "steel", "aerospace", "automotive", "electronics", "chemical", "pharma", "medical device", "packaging", "textile", "furniture", "foundry", "mill", "assembly", "production plant")),
)


#: How a company's industry was established. Only OBSERVED is evidence about
#: the employer; everything else is a hint that must not qualify a company.
OBSERVED = "OBSERVED"
INFERRED_HINT = "INFERRED_HINT"
UNKNOWN = "UNKNOWN"


def classify_industry(text: str | None) -> str | None:
    """Map a free-text industry label onto one of the ten client industries."""
    if not text:
        return None
    needle = str(text).strip().lower()
    if not needle:
        return None
    for industry, keywords in _INDUSTRY_PATTERNS:
        if any(k in needle for k in keywords):
            return industry
    return None


def infer_industry_from_titles(titles: list[str | None]) -> str | None:
    """Infer the client industry from the roles a company is advertising.

    Uses the SOC/O*NET-derived taxonomy, so this is grounded in the BLS
    occupation-to-industry mapping rather than in hand-written keywords.
    """
    votes: dict[str, int] = {}
    for title in titles:
        industry = taxonomy.industry_for_title(title)
        if industry:
            votes[industry] = votes.get(industry, 0) + 1
    if not votes:
        return None
    return max(votes.items(), key=lambda kv: kv[1])[0]


# ---------------------------------------------------------------------------
# Internal TA detection
# ---------------------------------------------------------------------------

_TA_ROLE_PATTERNS = (
    "talent acquisition",
    "recruiter",
    "recruiting manager",
    "recruiting coordinator",
    "sourcer",
    "head of talent",
    "talent partner",
    "people operations",
    "hris",
    "employer brand",
    "campus recruit",
    "technical recruiter",
)

_TA_SENIOR_PATTERNS = (
    "director of talent",
    "vp of talent",
    "vice president of talent",
    "head of talent acquisition",
    "chief people officer",
    "director of recruiting",
)


@dataclass
class InternalTASignal:
    """Evidence about the size/maturity of a company's in-house TA function."""

    ta_role_count: int = 0
    has_senior_ta_leader: bool = False
    matched_titles: list[str] = field(default_factory=list)

    @property
    def is_mature(self) -> bool:
        return self.ta_role_count >= 4 or (self.has_senior_ta_leader and self.ta_role_count >= 2)


def detect_internal_ta(job_titles: list[str | None]) -> InternalTASignal:
    """Detect a mature internal TA org from the roles a company is hiring for.

    Brief: *"Avoid companies with a large, mature internal Talent Acquisition
    organization when evidence supports that conclusion. A single HR Manager or
    HR Director is not automatically disqualifying."* Only true TA/recruiting
    roles count - a lone HR generalist deliberately does not.
    """
    signal = InternalTASignal()
    for title in job_titles:
        if not title:
            continue
        lowered = title.lower()
        if any(p in lowered for p in _TA_ROLE_PATTERNS):
            signal.ta_role_count += 1
            signal.matched_titles.append(title)
        if any(p in lowered for p in _TA_SENIOR_PATTERNS):
            signal.has_senior_ta_leader = True
    return signal


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


@dataclass
class CompanyProfile:
    """Company facts assembled from observed evidence (never invented)."""

    industry: str | None = None
    client_industry: str | None = None
    industry_known: bool = False
    industry_source: str | None = None

    employee_size_min: int | None = None
    employee_size_max: int | None = None
    size_known: bool = False
    size_source: str | None = None

    #: OBSERVED | INFERRED_HINT | UNKNOWN. ``industry_known`` is true only for
    #: OBSERVED - something the source actually published about the employer.
    industry_state: str = UNKNOWN

    internal_ta: InternalTASignal = field(default_factory=InternalTASignal)
    persistent_hiring_runs: int = 0

    def as_dict(self) -> dict:
        return {
            "industry": self.industry,
            "client_industry": self.client_industry,
            "industry_source": self.industry_source,
            "industry_state": self.industry_state,
            "employee_size_min": self.employee_size_min,
            "employee_size_max": self.employee_size_max,
            "size_source": self.size_source,
            "internal_ta_size": self.internal_ta.ta_role_count,
            "persistent_hiring_runs": self.persistent_hiring_runs,
        }


def _set_observed_industry(profile, raw_industry, source) -> None:
    """Record an industry the source published about the employer."""
    profile.industry = raw_industry
    profile.client_industry = classify_industry(raw_industry)
    profile.industry_known = True
    profile.industry_source = source
    profile.industry_state = OBSERVED


def _apply_industry(profile, company, fresh, titles) -> None:
    observed = company.observed_industry
    if observed:
        _set_observed_industry(profile, observed, "JOB_BOARD")
        return

    # The employer's own words in its posting are still the employer speaking.
    blob = " ".join((job.description or "")[:2000] for job, _ in fresh.fresh_jobs[:5])
    if classify_industry(blob):
        profile.industry = None
        profile.client_industry = classify_industry(blob)
        profile.industry_known = True
        profile.industry_source = "JOB_DESCRIPTION"
        profile.industry_state = OBSERVED
        return

    # From here down nothing was published about the employer. Hints are
    # recorded for triage and never counted as known.
    hint = infer_industry_from_titles(titles)
    if hint:
        profile.client_industry = hint
        profile.industry_known = False
        profile.industry_source = "OCCUPATION_TAXONOMY_HINT"
        profile.industry_state = INFERRED_HINT
        return

    if company.search_industry:
        # The industry of the query that surfaced this company: discovery
        # intent, not a fact about the employer.
        profile.client_industry = company.search_industry
        profile.industry_known = False
        profile.industry_source = "DISCOVERY_INTENT"
        profile.industry_state = INFERRED_HINT


def build_profile(
    fresh: FreshCompany,
    persistent_hiring_runs: int = 0,
    enrichment: dict | None = None,
) -> CompanyProfile:
    """Assemble a profile from discovery evidence, optionally topped up by enrichment."""
    profile = CompanyProfile(persistent_hiring_runs=persistent_hiring_runs)
    company = fresh.company
    titles = [job.title for job, _ in fresh.fresh_jobs]

    # --- Employee size ---------------------------------------------------
    lo, hi = parse_employee_range(company.observed_employee_count)
    if lo is not None or hi is not None:
        profile.employee_size_min, profile.employee_size_max = lo, hi
        profile.size_known = True
        profile.size_source = "JOB_BOARD"

    if enrichment:
        e_lo = enrichment.get("employee_size_min")
        e_hi = enrichment.get("employee_size_max")
        if e_lo is not None or e_hi is not None:
            profile.employee_size_min, profile.employee_size_max = e_lo, e_hi
            profile.size_known = True
            profile.size_source = enrichment.get("provider", "ENRICHMENT")

    # --- Industry --------------------------------------------------------
    # Precedence is strict and one-way: an industry the SOURCE published about
    # the employer is evidence and always wins. Inference from job titles is a
    # hint and may never overwrite it, upgrade itself to evidence, or qualify a
    # company on its own - "Claims Adjuster" does not make an insurer a
    # manufacturer.
    _apply_industry(profile, company, fresh, titles)

    if enrichment and enrichment.get("industry"):
        # A paid provider reports on the employer, so this is observed evidence
        # and outranks anything inferred locally.
        _set_observed_industry(
            profile, enrichment["industry"],
            enrichment.get("provider", "ENRICHMENT"),
        )

    # --- Internal TA -----------------------------------------------------
    profile.internal_ta = detect_internal_ta(titles)

    return profile
