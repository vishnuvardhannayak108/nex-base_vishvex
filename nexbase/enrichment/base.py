"""Enrichment provider interface, result types and the fallback vocabulary."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Provider outcome statuses. Each failure is named for what happened, because
# the fallback decision and the audit trail depend on the difference.
# ---------------------------------------------------------------------------
PROVIDER_SUCCESS = "PROVIDER_SUCCESS"
#: Identity resolved and something supplied, but a required gap remains.
PROVIDER_PARTIAL = "PROVIDER_PARTIAL"
#: Company or person not found, or identity could not be resolved safely.
PROVIDER_NO_MATCH = "PROVIDER_NO_MATCH"
PROVIDER_ERROR = "PROVIDER_ERROR"
PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
#: Not configured, no credentials, auth/scope refused, budget spent, or
#: disabled for the rest of the run.
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"

FAILURE_STATUSES = frozenset({
    PROVIDER_NO_MATCH, PROVIDER_ERROR, PROVIDER_TIMEOUT, PROVIDER_RATE_LIMITED,
    PROVIDER_UNAVAILABLE,
})


def failure_status(exc: Exception) -> str:
    """The provider status an exception from an HTTP call stands for."""
    if isinstance(exc, httpx.TimeoutException):
        return PROVIDER_TIMEOUT
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 429:
            return PROVIDER_RATE_LIMITED
        if code in (401, 403):
            return PROVIDER_UNAVAILABLE
    return PROVIDER_ERROR


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ProviderCall:
    """One request that left the process, as an enrichment_logs row."""

    endpoint: str
    payload: dict
    #: SUCCESS | NO_MATCH, or a PROVIDER_* failure status.
    status: str
    records: int = 0
    error: str | None = None
    #: Upper bound on credits this call may have charged.
    credit_cost: float = 0.0
    #: False when the provider charges nothing for this outcome (errors).
    billable: bool = False
    at: str = field(default_factory=utcnow)


@dataclass
class CompanyContext:
    """What a provider may use to identify a QUALIFIED company."""

    name: str | None
    name_normalized: str
    domain: str | None
    states: frozenset[str] = frozenset()


@dataclass
class EnrichmentNeed:
    """What this provider is asked to supply.

    ``full`` means find POC contacts at the company; ``people`` are existing
    POC contacts whose email is missing or weaker than a PERSONAL
    company-domain email; ``limit`` caps paid person lookups.
    """

    full: bool
    people: list[dict] = field(default_factory=list)
    limit: int = 3


@dataclass
class ProviderContact:
    """A person a provider attached to the company after its identity checks."""

    name: str | None
    title: str | None
    email: str | None = None
    profile_url: str | None = None
    person_id: Any = None
    extraction_method: str | None = None
    evidence_url: str | None = None
    confidence: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderOutcome:
    provider: str
    status: str
    reason: str | None = None
    #: How the company was identified: DOMAIN, NAME_AND_STATE, PERSON_ORGANIZATION_DOMAIN ...
    match_method: str | None = None
    provider_company_id: Any = None
    contacts: list[ProviderContact] = field(default_factory=list)
    calls: list[ProviderCall] = field(default_factory=list)


class FallbackProvider(ABC):
    """A provider the enrichment waterfall can consult."""

    name: str = "PROVIDER"
    #: Stored contacts from this provider are reused for this many days (None: never).
    refresh_days: int | None = None
    max_companies_per_run: int = 0
    max_person_lookups_per_company: int = 0

    @abstractmethod
    def is_configured(self) -> bool: ...

    @abstractmethod
    def find(self, company: CompanyContext, need: EnrichmentNeed,
             existing: list[dict]) -> ProviderOutcome:
        """Look for what ``need`` asks for; never raises for provider failures."""


# ---------------------------------------------------------------------------
# Company-level provider interface (kept for the providers' company lookups)
# ---------------------------------------------------------------------------
@dataclass
class EnrichedContact:
    """A decision-maker returned by a paid provider."""

    name: str | None = None
    title: str | None = None
    email: str | None = None
    profile_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class EnrichmentData:
    industry: str | None = None
    employee_size_min: int | None = None
    employee_size_max: int | None = None
    employee_count: int | None = None
    location: str | None = None
    domain: str | None = None
    contacts: list[EnrichedContact] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class EnrichmentCallResult:
    provider: str
    ok: bool
    data: EnrichmentData | None = None
    error: str | None = None
    credit_cost: float = 0.0
    #: False when no paid call actually left the process (not configured,
    #: not approved). Those must not be counted as spend.
    billable: bool = False
    endpoint: str | None = None


class EnrichmentProvider(ABC):
    """A paid enrichment data source."""

    name: str = "ENRICHMENT"

    @abstractmethod
    def is_configured(self) -> bool: ...

    @abstractmethod
    def enrich(
        self, company_name: str, domain: str | None, contacts: list[dict]
    ) -> EnrichmentCallResult:
        """Attempt enrichment for a company; returns a structured result."""


def bucket_to_range(count: int | None) -> tuple[int | None, int | None]:
    """Turn an exact headcount into the band the brief reasons about."""
    if count is None:
        return None, None
    return count, count
