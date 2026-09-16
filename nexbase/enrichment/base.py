"""Enrichment provider interface and result types."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


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
