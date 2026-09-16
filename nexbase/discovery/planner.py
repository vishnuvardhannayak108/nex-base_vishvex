"""USA-wide planner: Sector + Job in, a deterministic discovery plan out.

The user states two things and nothing else. The planner derives the rest:

* **Title variants** from the internal O*NET/BLS taxonomy. A variant must sit in
  the same occupation (SOC code) as the requested job *and* share a distinctive
  word with it, so "Welder" widens to "MIG Welder" but never to "Solderer".
  Terms containing every word of the job are variants too. A vague job
  ("Supervisor") or one the taxonomy does not know is searched as typed and
  never widened - nothing is guessed.
* **Geographic coverage** of the whole USA: the country first, and every state
  plus DC as the partition a source falls back to when a nationwide query hits
  a result cap. The source registry walks that hierarchy.

Same sector and job in, same plan out (apart from ``run_key``).
"""
from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import lru_cache

from nexbase.config import Settings, get_settings
from nexbase.core.enums import ClientIndustry
from nexbase.core.errors import DiscoveryError
from nexbase.discovery import taxonomy
from nexbase.logging_setup import get_logger

COUNTRY, STATE = "COUNTRY", "STATE"

#: Words that describe a level or a function in general, not the job itself.
#: A variant must share something more specific than these.
GENERIC_TITLE_WORDS = frozenset({
    "a", "and", "apprentice", "assistant", "associate", "chief", "coordinator",
    "director", "entry", "for", "general", "head", "helper", "i", "ii", "iii",
    "in", "iv", "jr", "junior", "lead", "leader", "level", "manager", "of",
    "officer", "operator", "senior", "specialist", "sr", "staff", "supervisor",
    "technician", "the", "trainee", "worker",
})

MAX_JOB_LENGTH = 120


@dataclass(frozen=True)
class GeoCell:
    """One unit of geographic coverage."""

    code: str
    name: str
    level: str


NATIONWIDE = GeoCell("US", "USA", COUNTRY)

#: The 50 states and DC: the partition of NATIONWIDE.
US_STATES: tuple[GeoCell, ...] = tuple(GeoCell(code, name, STATE) for code, name in (
    ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"), ("AR", "Arkansas"),
    ("CA", "California"), ("CO", "Colorado"), ("CT", "Connecticut"),
    ("DE", "Delaware"), ("DC", "District of Columbia"), ("FL", "Florida"),
    ("GA", "Georgia"), ("HI", "Hawaii"), ("ID", "Idaho"), ("IL", "Illinois"),
    ("IN", "Indiana"), ("IA", "Iowa"), ("KS", "Kansas"), ("KY", "Kentucky"),
    ("LA", "Louisiana"), ("ME", "Maine"), ("MD", "Maryland"),
    ("MA", "Massachusetts"), ("MI", "Michigan"), ("MN", "Minnesota"),
    ("MS", "Mississippi"), ("MO", "Missouri"), ("MT", "Montana"),
    ("NE", "Nebraska"), ("NV", "Nevada"), ("NH", "New Hampshire"),
    ("NJ", "New Jersey"), ("NM", "New Mexico"), ("NY", "New York"),
    ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"),
    ("OK", "Oklahoma"), ("OR", "Oregon"), ("PA", "Pennsylvania"),
    ("RI", "Rhode Island"), ("SC", "South Carolina"), ("SD", "South Dakota"),
    ("TN", "Tennessee"), ("TX", "Texas"), ("UT", "Utah"), ("VT", "Vermont"),
    ("VA", "Virginia"), ("WA", "Washington"), ("WV", "West Virginia"),
    ("WI", "Wisconsin"), ("WY", "Wyoming"),
))


def known_sectors() -> list[str]:
    """The sectors a run may target."""
    return [i.value for i in ClientIndustry]


@dataclass(frozen=True)
class TitleVariant:
    title: str
    #: INPUT (what the user typed) or TAXONOMY (derived from it).
    origin: str
    soc_code: str | None = None


@dataclass
class DiscoveryPlan:
    run_key: str
    sector: str
    job: str
    titles: list[TitleVariant]
    hours_old: int
    geo_root: GeoCell = NATIONWIDE
    geo_partitions: tuple[GeoCell, ...] = US_STATES
    #: SOC codes the job matched exactly in the taxonomy. Empty when unknown.
    matched_soc_codes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_key": self.run_key,
            "sector": self.sector,
            "job": self.job,
            "titles": [asdict(t) for t in self.titles],
            "hours_old": self.hours_old,
            "geo_root": asdict(self.geo_root),
            "geo_partitions": [g.code for g in self.geo_partitions],
            "matched_soc_codes": self.matched_soc_codes,
        }


class DiscoveryPlanner:
    """Builds a USA-wide plan from a sector and a job."""

    def __init__(self, settings: Settings | None = None, logger=None) -> None:
        self.settings = settings or get_settings()
        self.log = logger or get_logger("nexbase.discovery.planner")

    def plan(self, sector: str, job: str) -> DiscoveryPlan:
        sector = (sector or "").strip()
        if sector not in known_sectors():
            raise DiscoveryError(
                f"unknown sector {sector!r}; expected one of {known_sectors()}")
        job = " ".join((job or "").split())
        if not job:
            raise DiscoveryError("a job title or job category is required")
        if len(job) > MAX_JOB_LENGTH:
            raise DiscoveryError(f"job must be at most {MAX_JOB_LENGTH} characters")

        matched, variants = expand_title(
            job, sector, self.settings.planner_max_title_variants)
        plan = DiscoveryPlan(
            run_key=(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                     + "-" + uuid.uuid4().hex[:6]),
            sector=sector,
            job=job,
            titles=variants,
            hours_old=self.settings.freshness_max_days * 24,
            matched_soc_codes=matched,
        )
        self.log.info(
            "discovery_plan_built", run_key=plan.run_key, sector=sector, job=job,
            titles=[t.title for t in variants], matched_soc_codes=matched,
            geo_partitions=len(plan.geo_partitions),
        )
        return plan


# ---------------------------------------------------------------------------
# Title expansion
# ---------------------------------------------------------------------------
@lru_cache(maxsize=None)
def _tokens(title: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", taxonomy._title_key(title)))


@lru_cache(maxsize=None)
def _sector_socs(sector: str) -> frozenset[str]:
    """SOC codes that staff the sector's industries, per the BLS crosswalk."""
    return frozenset(
        soc for industry in taxonomy.load_industries()
        if industry.client_industry == sector for soc in industry.soc_codes)


def expand_title(job: str, sector: str, limit: int) -> tuple[list[str], list[TitleVariant]]:
    """Return ``(matched_soc_codes, variants)``; the job itself is always first."""
    job_tokens = _tokens(job)
    distinctive = set(job_tokens) - GENERIC_TITLE_WORDS
    variants = [TitleVariant(job, "INPUT")]
    if not distinctive or limit <= 1:
        return [], variants[:max(1, limit)]

    terms = taxonomy.load_search_terms()
    matched = sorted({t.soc_code for t in terms if _tokens(t.term) == job_tokens})
    sector_socs = _sector_socs(sector)

    seen = {job_tokens}
    ranked = []
    for term in terms:
        tokens = _tokens(term.term)
        if tokens in seen or not taxonomy.is_searchable(term.term):
            continue
        same_occupation = term.soc_code in matched
        shared = distinctive & set(tokens)
        if not ((same_occupation and shared) or set(job_tokens) <= set(tokens)):
            continue
        seen.add(tokens)
        ranked.append((
            0 if same_occupation else 1,
            0 if term.soc_code in sector_socs else 1,
            -len(shared),
            len(term.term),
            term.term.lower(),
            term,
        ))

    for *_, term in sorted(ranked, key=lambda r: r[:5])[:limit - 1]:
        variants.append(TitleVariant(term.term, "TAXONOMY", term.soc_code))
    return matched, variants


__all__ = [
    "COUNTRY",
    "NATIONWIDE",
    "STATE",
    "US_STATES",
    "DiscoveryPlan",
    "DiscoveryPlanner",
    "GeoCell",
    "TitleVariant",
    "expand_title",
    "known_sectors",
]
