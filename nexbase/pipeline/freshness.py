"""Module 5: Freshness Filter.

Brief:
  * Highest priority: approximately **5 hours to 7 days** old.
  * Priority 2: older than 7 days but within 14 days.
  * Priority 3: **approaching 14 days**.
  * Hard exclusion: older than 14 days.

The 5-hour floor is real and implemented: a posting younger than that is kept
but demoted to P2, because sub-5-hour listings are frequently mid-publication
(incomplete fields, no apply URL yet) and the brief singles out 5h as the start
of the highest-priority window rather than "as fresh as possible".

Exact posting dates are preserved; missing and future dates are rejected with a
reason rather than guessed, so every decision stays auditable.
"""
from __future__ import annotations

import re

from dataclasses import dataclass
from datetime import datetime, timezone

from nexbase.config import Settings, get_settings
from nexbase.core.enums import FreshnessPriority
from nexbase.logging_setup import get_logger
from nexbase.pipeline.dedupe import CompanyAggregate
from nexbase.pipeline.normalize import NormalizedJob


@dataclass
class FreshnessResult:
    keep: bool
    priority: int | None
    age_days: float | None
    reason: str | None

    @property
    def age_hours(self) -> float | None:
        return None if self.age_days is None else self.age_days * 24.0


def compute_age_days(posted_at: datetime, now: datetime) -> float:
    return (now - posted_at).total_seconds() / 86400.0


#: Employment types the client excludes outright: "Don't include contract,
#: part-time, temporary, contract-to-hire, freelance, independent contractor or
#: seasonal roles." Matched as substrings because sources spell these many ways
#: ("Contract", "contract-to-hire", "Temp-to-Perm").
EXCLUDED_EMPLOYMENT_TOKENS = (
    "part time", "parttime", "part-time",
    "contract", "contractor", "c2h", "corp to corp", "corp-to-corp",
    "temp", "temporary", "seasonal", "freelance", "intern", "volunteer",
    "per diem", "perdiem", "casual",
)

#: Spellings that positively identify a full-time role.
FULL_TIME_TOKENS = ("full time", "fulltime", "full-time", "permanent", "regular")


def employment_verdict(employment_type: str | None) -> tuple[bool, str | None]:
    """Return ``(acceptable, reason)`` for a source-reported employment type.

    An unstated type is accepted rather than guessed at: most boards publish
    nothing, and rejecting silence would discard the majority of real
    full-time postings. Only a type the source actually stated can exclude.
    """
    if not employment_type:
        return True, None
    # schema.org spells these PART_TIME / FULL_TIME, boards spell them
    # "Part-time" / "part time". Fold every separator to a space so one token
    # list covers all of them - without this PART_TIME was silently accepted.
    value = re.sub(r"[_\-/]+", " ", str(employment_type).strip().lower())
    value = re.sub(r"\s+", " ", value)
    if any(token in value for token in FULL_TIME_TOKENS):
        return True, None
    for token in EXCLUDED_EMPLOYMENT_TOKENS:
        if token in value:
            return False, f"NOT_FULL_TIME:{value[:40]}"
    return True, None


#: Location tails that are definitely not the United States. The client targets
#: US companies only; a posting whose own location names another country is
#: rejected rather than normalised into one.
NON_US_MARKERS = (
    "canada", "united kingdom", "india", "australia", "germany", "france",
    "mexico", "brazil", "ireland", "singapore", "philippines", "netherlands",
    "spain", "italy", "poland", "japan", "china", "remote, uk", ", uk", ", ca,",
)


def is_us_location(country: str | None, location: str | None) -> bool:
    """True unless the posting positively identifies a non-US country."""
    if country:
        value = str(country).strip().lower()
        if value in ("us", "usa", "united states", "united states of america"):
            return True
        if len(value) >= 2:
            return False
    text = (location or "").strip().lower()
    return not any(marker in text for marker in NON_US_MARKERS)


def evaluate_freshness(
    job: NormalizedJob,
    now: datetime | None = None,
    max_days: int = 14,
    priority1_max_days: int = 7,
    priority1_min_hours: float = 5.0,
    priority2_max_days: int = 12,
) -> FreshnessResult:
    """Evaluate a single job against the freshness rules."""
    now = now or datetime.now(timezone.utc)

    # Client rules, applied before age: a part-time or non-US posting is out of
    # scope no matter how fresh it is.
    acceptable, reason = employment_verdict(getattr(job, "employment_type", None))
    if not acceptable:
        return FreshnessResult(False, None, None, reason)
    if not is_us_location(getattr(job, "country", None), job.location):
        return FreshnessResult(False, None, None, "NOT_US")

    if job.posted_at is None:
        return FreshnessResult(False, None, None, "MISSING_POSTING_DATE")

    posted_at = job.posted_at
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    age_days = compute_age_days(posted_at, now)
    age_hours = age_days * 24.0

    if age_days < 0:
        return FreshnessResult(False, None, age_days, "FUTURE_POSTING_DATE")
    if age_days > max_days:
        return FreshnessResult(False, None, age_days, "STALE")

    # Brief's highest-priority window opens at ~5 hours.
    if age_hours < priority1_min_hours:
        return FreshnessResult(True, int(FreshnessPriority.P2), age_days, "BELOW_P1_MIN_AGE")
    if age_days <= priority1_max_days:
        return FreshnessResult(True, int(FreshnessPriority.P1), age_days, None)
    if age_days <= priority2_max_days:
        return FreshnessResult(True, int(FreshnessPriority.P2), age_days, None)
    # 12-14 days: "approaching 14 days".
    return FreshnessResult(True, int(FreshnessPriority.P3), age_days, "APPROACHING_CUTOFF")


@dataclass
class FreshCompany:
    """A deduplicated company after freshness filtering."""

    company: CompanyAggregate
    fresh_jobs: list[tuple[NormalizedJob, FreshnessResult]]
    rejected_jobs: list[tuple[NormalizedJob, FreshnessResult]]

    @property
    def hiring_intensity(self) -> int:
        return len(self.fresh_jobs)

    @property
    def best_priority(self) -> int | None:
        priorities = [r.priority for _, r in self.fresh_jobs if r.priority is not None]
        return min(priorities) if priorities else None

    @property
    def min_age_days(self) -> float | None:
        ages = [r.age_days for _, r in self.fresh_jobs if r.age_days is not None]
        return min(ages) if ages else None


class FreshnessFilter:
    """Applies the freshness filter to deduplicated company aggregates."""

    def __init__(self, settings: Settings | None = None, logger=None) -> None:
        self.settings = settings or get_settings()
        self.log = logger or get_logger("nexbase.pipeline.freshness")

    def evaluate(self, job: NormalizedJob, now: datetime | None = None) -> FreshnessResult:
        return evaluate_freshness(
            job,
            now=now,
            max_days=self.settings.freshness_max_days,
            priority1_max_days=self.settings.freshness_priority1_max_days,
            priority1_min_hours=self.settings.freshness_priority1_min_hours,
            priority2_max_days=self.settings.freshness_priority2_max_days,
        )

    def filter(
        self, companies: list[CompanyAggregate], now: datetime | None = None
    ) -> list[FreshCompany]:
        out: list[FreshCompany] = []
        rejected_companies = 0
        for company in companies:
            fresh: list[tuple[NormalizedJob, FreshnessResult]] = []
            rejected: list[tuple[NormalizedJob, FreshnessResult]] = []
            for job in company.jobs:
                result = self.evaluate(job, now=now)
                (fresh if result.keep else rejected).append((job, result))
            if fresh:
                out.append(FreshCompany(company, fresh, rejected))
            else:
                rejected_companies += 1
        self.log.info(
            "freshness_complete",
            companies_in=len(companies),
            companies_kept=len(out),
            companies_rejected=rejected_companies,
        )
        return out
