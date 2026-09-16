"""Contact data models."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nexbase.core.enums import ContactPriority


@dataclass
class ContactCandidate:
    """A person discovered at a company, before ranking/selection."""

    name: str | None
    title: str | None
    title_priority: int | None  # 1..4
    email: str | None = None
    source_type: str | None = None
    source_priority: int | None = None
    discovery_stage: str | None = None
    url: str | None = None
    profile_url: str | None = None
    confidence: float = 0.0
    #: Populated by the ranker. Declared here so it survives ``asdict()`` and
    #: is never an undeclared ad-hoc attribute.
    rank_score: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def meaningful(self) -> bool:
        """A contact is 'meaningful' only if it has a name and an inferred title."""
        return bool(self.name and self.title_priority)

    @property
    def priority(self) -> int:
        return int(self.title_priority or ContactPriority.P4.value)

    @property
    def identity_key(self) -> str:
        return (self.name or "").strip().lower()
