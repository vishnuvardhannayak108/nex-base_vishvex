"""Why discovery stopped, per source and per query.

A source returning nothing is not the same fact as "no such jobs exist". It
may have been blocked by Cloudflare, refused by robots, rate limited, capped by
its own API, capped by *our* budget, or simply have no more pages. Those are
different problems with different fixes, and collapsing them into a count of
zero hides all of them.

Every adapter records one :class:`SourceOutcome` per (query, location) it runs,
and the runner reports them. ``stop_reason`` always distinguishes an upstream
limitation from a NexBase operational one.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

#: The source had nothing left to give: a page came back with no new rows.
SOURCE_EXHAUSTED = "SOURCE_EXHAUSTED"
#: The source itself refuses to go further (API page ceiling, result ceiling).
SOURCE_LIMIT_REACHED = "SOURCE_LIMIT_REACHED"
#: *We* stopped, not the source. An explicit operational budget was hit.
BUDGET_REACHED = "BUDGET_REACHED"
#: Anti-bot, CAPTCHA, 403, or an access restriction.
BLOCKED = "BLOCKED"
#: robots.txt disallowed the path.
ROBOTS_RESTRICTED = "ROBOTS_RESTRICTED"
#: 429/503 backoff.
RATE_LIMITED = "RATE_LIMITED"
#: Parser, HTTP, or adapter failure.
ERROR = "ERROR"

#: Reasons that mean "the source could not or would not give us more".
UPSTREAM_REASONS = frozenset({
    SOURCE_EXHAUSTED, SOURCE_LIMIT_REACHED, BLOCKED, ROBOTS_RESTRICTED,
    RATE_LIMITED, ERROR,
})
#: Reasons that mean "NexBase chose to stop". Exactly one, deliberately.
OPERATIONAL_REASONS = frozenset({BUDGET_REACHED})


@dataclass
class SourceOutcome:
    """One (source, query, location) execution and how it ended."""

    source: str
    query: str | None = None
    location_scope: str | None = None
    pages: int = 0
    offset: int = 0
    jobs_returned: int = 0
    jobs_accepted: int = 0
    duplicates: int = 0
    stop_reason: str = SOURCE_EXHAUSTED
    error_type: str | None = None
    error_message: str | None = None
    runtime_seconds: float = 0.0

    @property
    def source_exhausted(self) -> bool:
        return self.stop_reason == SOURCE_EXHAUSTED

    @property
    def source_limit_reached(self) -> bool:
        return self.stop_reason == SOURCE_LIMIT_REACHED

    @property
    def blocked(self) -> bool:
        return self.stop_reason == BLOCKED

    @property
    def robots_restricted(self) -> bool:
        return self.stop_reason == ROBOTS_RESTRICTED

    @property
    def limited_by_source(self) -> bool:
        """True when the ceiling was upstream, not ours."""
        return self.stop_reason in UPSTREAM_REASONS

    @property
    def limited_by_nexbase(self) -> bool:
        """True when we stopped a source that still had more to give."""
        return self.stop_reason in OPERATIONAL_REASONS

    def as_dict(self) -> dict:
        data = asdict(self)
        data.update({
            "source_exhausted": self.source_exhausted,
            "source_limit_reached": self.source_limit_reached,
            "blocked": self.blocked,
            "robots_restricted": self.robots_restricted,
            "limited_by_source": self.limited_by_source,
            "limited_by_nexbase": self.limited_by_nexbase,
        })
        return data


def classify_source_log(messages: list[str]) -> tuple[str, str | None]:
    """Read a source library's own warnings and say why it gave us nothing.

    Some scrapers report failure by logging and returning an empty result, so
    the empty result is all NexBase would otherwise see. Returns
    ``(stop_reason, message)``; an empty list means nothing was reported and the
    caller keeps its own verdict.
    """
    if not messages:
        return SOURCE_EXHAUSTED, None
    joined = " | ".join(messages)
    text = joined.lower()
    status = None
    match = re.search(r"status code (\d{3})", text)
    if match:
        status = int(match.group(1))
    if status in (429,) or "rate limit" in text or "too many" in text:
        return RATE_LIMITED, joined
    if status in (401, 403) or "forbidden" in text or "captcha" in text or "cloudflare" in text:
        return BLOCKED, joined
    if "robots" in text:
        return ROBOTS_RESTRICTED, joined
    if status is not None and status >= 400:
        return ERROR, joined
    return ERROR, joined


def classify_fetch_failure(error: str | None, status: int | None = None) -> str:
    """Map an access-layer failure onto a stop reason.

    The access layer already names what happened; this keeps the mapping in one
    place so every adapter reports the same vocabulary.
    """
    text = (error or "").upper()
    if "ROBOTS" in text:
        return ROBOTS_RESTRICTED
    if status in (429, 503) or "RATE" in text or "TOO MANY" in text:
        return RATE_LIMITED
    if status in (401, 403) or "BLOCK" in text or "CAPTCHA" in text or "FORBIDDEN" in text:
        return BLOCKED
    if text:
        return ERROR
    return SOURCE_EXHAUSTED


@dataclass
class CoverageReport:
    """Every outcome from one run, plus the totals worth reading at a glance."""

    outcomes: list[SourceOutcome] = field(default_factory=list)

    def add(self, outcome: SourceOutcome) -> SourceOutcome:
        self.outcomes.append(outcome)
        return outcome

    def by_source(self) -> dict[str, dict]:
        rolled: dict[str, dict] = {}
        for outcome in self.outcomes:
            row = rolled.setdefault(outcome.source, {
                "queries": 0, "pages": 0, "jobs_returned": 0, "jobs_accepted": 0,
                "duplicates": 0, "stop_reasons": {}, "errors": 0,
                "limited_by_nexbase": 0, "limited_by_source": 0,
                "last_error": None,
            })
            row["queries"] += 1
            row["pages"] += outcome.pages
            row["jobs_returned"] += outcome.jobs_returned
            row["jobs_accepted"] += outcome.jobs_accepted
            row["duplicates"] += outcome.duplicates
            row["stop_reasons"][outcome.stop_reason] = (
                row["stop_reasons"].get(outcome.stop_reason, 0) + 1)
            if outcome.error_message:
                row["errors"] += 1
                row["last_error"] = outcome.error_message
            if outcome.limited_by_nexbase:
                row["limited_by_nexbase"] += 1
            if outcome.limited_by_source:
                row["limited_by_source"] += 1
        return rolled

    def as_dict(self) -> dict:
        return {
            "by_source": self.by_source(),
            "outcomes": [o.as_dict() for o in self.outcomes],
        }
