"""Discovery planner: turns the operator's configuration into a run.

NexBase is **not** autonomous. The operator chooses the sector, the location,
the search terms, the employee band, the freshness window and the sources; this
module's only job is to turn that choice into a deterministic execution plan and
to record what each probe actually did in ``discovery_runs``.

It deliberately does **not** invent industries, locations, terms, budget
allocations or an exploration strategy. An earlier version did all five from
SOC/O*NET data; that behaviour is gone from the product front door and may
return later as a separate, explicitly-chosen capability. The taxonomy itself is
untouched and still serves sector lists, term suggestions and classification -
see :mod:`nexbase.discovery.taxonomy`.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from nexbase.config import Settings, get_settings
from nexbase.core.enums import ClientIndustry
from nexbase.core.errors import DiscoveryError
from nexbase.discovery import taxonomy
from nexbase.logging_setup import get_logger

#: The default scope. "USA" means *nationwide United States*, and it is passed
#: to the sources as one scope. It is NOT expanded into 50 states or a metro
#: list: those datasets exist to help a human narrow a search, never to define
#: what "nationwide" means.
NATIONWIDE = "USA"

#: Spellings a human may type for the nationwide scope.
_NATIONWIDE_ALIASES = frozenset({
    "usa", "us", "u.s.", "u.s.a.", "united states",
    "united states of america", "nationwide", "all", "anywhere",
})


def is_nationwide(location: str | None) -> bool:
    """True when this location means "the whole United States"."""
    return (location or "").strip().lower().rstrip(".") in {
        alias.rstrip(".") for alias in _NATIONWIDE_ALIASES
    }


def known_sectors() -> list[str]:
    """Sector values a run may name, for a dropdown or validation."""
    return [i.value for i in ClientIndustry]


@dataclass(frozen=True)
class DiscoveryConfig:
    """One run's configuration, exactly as the operator chose it.

    This is authoritative. Nothing downstream may widen, narrow or substitute
    any field: an empty ``sector`` means "no sector stated", not "pick one".
    """

    search_terms: tuple[str, ...]
    sector: str | None = None
    location: str = NATIONWIDE
    size_min: int | None = None
    size_max: int | None = None
    freshness_days: int | None = None
    #: Source selection. Empty means "the configured defaults for this source".
    jobspy_sites: tuple[str, ...] = ()
    board_sites: tuple[str, ...] = ()
    ats_slices: tuple[str, ...] = ()
    #: Operational coverage budgets. See DiscoveryPlan for what they bound.
    max_pages_per_query: int | None = None
    max_results_per_query: int | None = None

    @property
    def nationwide(self) -> bool:
        return is_nationwide(self.location)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["nationwide"] = self.nationwide
        return data


def build_config(
    search_terms,
    sector: str | None = None,
    location: str | None = None,
    size_min: int | None = None,
    size_max: int | None = None,
    freshness_days: int | None = None,
    jobspy_sites=None,
    board_sites=None,
    ats_slices=None,
    max_pages_per_query: int | None = None,
    max_results_per_query: int | None = None,
) -> DiscoveryConfig:
    """Validate and normalise an operator's choices into a DiscoveryConfig.

    Raises :class:`DiscoveryError` rather than guessing: a run with no search
    term would otherwise quietly become a run with terms we invented.
    """
    terms = tuple(dict.fromkeys(
        t.strip() for t in (search_terms or []) if t and t.strip()))
    if not terms:
        raise DiscoveryError(
            "at least one search term is required; NexBase does not generate "
            "them. taxonomy.suggest_terms_for_sector() offers candidates.")

    if sector is not None:
        sector = sector.strip() or None
    if sector is not None and sector not in known_sectors():
        raise DiscoveryError(
            f"unknown sector {sector!r}; expected one of {known_sectors()}")

    location = (location or NATIONWIDE).strip() or NATIONWIDE

    if size_min is not None and size_max is not None and size_min > size_max:
        raise DiscoveryError(
            f"size_min ({size_min}) must not exceed size_max ({size_max})")
    if freshness_days is not None and freshness_days < 1:
        raise DiscoveryError("freshness_days must be at least 1")

    return DiscoveryConfig(
        search_terms=terms,
        sector=sector,
        location=location,
        size_min=size_min,
        size_max=size_max,
        freshness_days=freshness_days,
        jobspy_sites=tuple(jobspy_sites or ()),
        board_sites=tuple(board_sites or ()),
        ats_slices=tuple(ats_slices or ()),
        max_pages_per_query=max_pages_per_query,
        max_results_per_query=max_results_per_query,
    )


@dataclass
class DiscoveryProbe:
    """One planned search: a (search_term, location) pair the operator chose.

    ``client_industry`` is the sector the operator selected for this run. It is
    search *intent* and is never asserted as a fact about an employer.
    ``naics3``/``naics_title`` stay available for classification and evidence.
    """

    client_industry: str
    search_term: str
    location: str
    freshness_window_hours: int = 336
    naics3: str | None = None
    naics_title: str | None = None

    @property
    def nationwide(self) -> bool:
        return is_nationwide(self.location)

    @property
    def tuple(self) -> tuple[str, str, int]:
        """The discovery tuple: (search term, US location, freshness window)."""
        return (self.search_term, self.location, self.freshness_window_hours)

    @property
    def key(self) -> str:
        raw = (
            f"{self.client_industry}|{self.naics3}|{self.search_term}|"
            f"{self.location}|{self.freshness_window_hours}"
        ).lower()
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class DiscoveryPlan:
    """A deterministic execution plan. One probe per configured search term."""

    run_key: str
    probes: list[DiscoveryProbe] = field(default_factory=list)
    config: DiscoveryConfig | None = None
    hours_old: int | None = None
    sites: list[str] = field(default_factory=list)
    board_sites: list[str] = field(default_factory=list)
    ats_slices: list[str] = field(default_factory=list)
    size_min: int | None = None
    size_max: int | None = None
    #: Operational coverage budgets, applied per (source, query). These are
    #: NexBase limits, not source limits, and a run that hits one reports
    #: BUDGET_REACHED so the two are never confused.
    max_pages_per_query: int = 25
    max_results_per_query: int = 1000

    @property
    def sector(self) -> str | None:
        return self.config.sector if self.config else None

    @property
    def nationwide(self) -> bool:
        return bool(self.config and self.config.nationwide)

    def terms_for(self, industry: str) -> list[str]:
        return [p.search_term for p in self.probes if p.client_industry == industry]

    @property
    def tuples(self) -> list[tuple[str, str, int]]:
        return [p.tuple for p in self.probes]

    @property
    def locations(self) -> list[str]:
        seen: dict[str, None] = {}
        for probe in self.probes:
            seen.setdefault(probe.location, None)
        return list(seen)

    def to_dict(self) -> dict:
        return {
            "run_key": self.run_key,
            "sector": self.sector,
            "location": self.config.location if self.config else None,
            "nationwide": self.nationwide,
            "search_terms": list(self.config.search_terms) if self.config else [],
            "hours_old": self.hours_old,
            "sites": self.sites,
            "board_sites": self.board_sites,
            "ats_slices": self.ats_slices,
            "size_min": self.size_min,
            "size_max": self.size_max,
            "max_pages_per_query": self.max_pages_per_query,
            "max_results_per_query": self.max_results_per_query,
            "probe_count": len(self.probes),
            "probes": [asdict(p) for p in self.probes],
        }


class DiscoveryPlanner:
    """Builds a plan from an operator's configuration and audits each probe."""

    def __init__(self, settings: Settings | None = None, repo=None, logger=None) -> None:
        self.settings = settings or get_settings()
        self.repo = repo
        self.log = logger or get_logger("nexbase.discovery.planner")

    def plan(self, config: DiscoveryConfig) -> DiscoveryPlan:
        """Turn one configuration into one deterministic plan.

        Same configuration in, same plan out: there is no sampling, no random
        seed and no allocation step.
        """
        if not isinstance(config, DiscoveryConfig):
            raise DiscoveryError(
                "DiscoveryPlanner.plan() takes a DiscoveryConfig. Autonomous "
                "planning was removed from the product front door; build a "
                "config with planner.build_config().")

        freshness_days = config.freshness_days or self.settings.freshness_max_days
        hours_old = freshness_days * 24
        sector = config.sector or ""

        probes = [
            DiscoveryProbe(
                client_industry=sector,
                search_term=term,
                location=config.location,
                freshness_window_hours=hours_old,
            )
            for term in config.search_terms
        ]

        plan = DiscoveryPlan(
            run_key=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            probes=probes,
            config=config,
            hours_old=hours_old,
            sites=list(config.jobspy_sites or self.settings.jobspy_sites),
            board_sites=list(config.board_sites or self.settings.board_sites),
            ats_slices=list(config.ats_slices or self.settings.ats_slices),
            size_min=config.size_min if config.size_min is not None
            else self.settings.size_filter_min,
            size_max=config.size_max if config.size_max is not None
            else self.settings.size_filter_max,
            max_pages_per_query=(
                config.max_pages_per_query
                if config.max_pages_per_query is not None
                else self.settings.discovery_max_pages_per_query),
            max_results_per_query=(
                config.max_results_per_query
                if config.max_results_per_query is not None
                else self.settings.discovery_max_results_per_query),
        )
        self.log.info(
            "discovery_plan_built",
            run_key=plan.run_key,
            sector=sector or None,
            location=config.location,
            nationwide=config.nationwide,
            probes=len(probes),
            hours_old=hours_old,
            sites=plan.sites,
            board_sites=plan.board_sites,
            ats_slices=len(plan.ats_slices),
        )
        return plan

    def record(self, plan: DiscoveryPlan, probe: DiscoveryProbe, source: str) -> str | None:
        """Open a ``discovery_runs`` row for one probe. Returns its id."""
        if self.repo is None:
            return None
        return self.repo.insert_discovery_run(
            {
                "run_key": plan.run_key,
                "source": source,
                "search_term": probe.search_term,
                "location": probe.location,
                "client_industry": probe.client_industry or None,
                "params": {
                    "hours_old": plan.hours_old,
                    "probe_key": probe.key,
                    "nationwide": probe.nationwide,
                    "naics3": probe.naics3,
                    "naics_title": probe.naics_title,
                },
                "status": "RUNNING",
            }
        )

    def close(
        self,
        run_id: str | None,
        jobs_found: int,
        status: str = "COMPLETE",
        error: str | None = None,
        outcome=None,
    ) -> None:
        """Close a ``discovery_runs`` row, recording why the source stopped."""
        if self.repo is None or run_id is None:
            return
        data = {
            "jobs_found": jobs_found,
            "status": status,
            "error": error,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        if outcome is not None:
            data["params"] = outcome.as_dict()
        self.repo.update_discovery_run(run_id, data)


__all__ = [
    "NATIONWIDE",
    "DiscoveryConfig",
    "DiscoveryPlan",
    "DiscoveryPlanner",
    "DiscoveryProbe",
    "build_config",
    "is_nationwide",
    "known_sectors",
    "taxonomy",
]
