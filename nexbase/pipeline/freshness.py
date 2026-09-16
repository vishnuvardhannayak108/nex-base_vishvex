"""Freshness filter: keep only postings from the last 14 days.

Runs on deduplicated postings, before company identification, so a company is
only ever built from postings that are actually fresh.

Every decision carries a reason:

* **Dated, timestamp** - age in hours; kept when at most ``freshness_max_days``.
* **Dated, date only** (``date_precision == "DAY"``) - age in calendar days, so
  a job posted on the 2nd is 14 days old on the 16th however late in the day.
* **Future date** - rejected (``FUTURE_POSTING_DATE``); timestamps get one hour
  of clock-skew tolerance.
* **Missing date** - handled explicitly, never guessed. When the source itself
  applied a date filter no wider than the window (JobSpy ``hours_old``,
  SimplyHired ``t``, Talent.com ``date``, Apify date inputs), the posting is
  provably inside it and is kept as ``UNDATED_WITHIN_SOURCE_WINDOW`` with an
  unknown age. Otherwise it is rejected as ``MISSING_POSTING_DATE``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from nexbase.config import Settings, get_settings
from nexbase.logging_setup import get_logger
from nexbase.pipeline.normalize import NormalizedJob

#: Provenance key the source registry stamps with the date filter it applied.
SOURCE_WINDOW_KEY = "source_date_window_hours"
_CLOCK_SKEW_DAYS = 1 / 24


@dataclass
class FreshnessResult:
    keep: bool
    age_days: float | None
    reason: str | None


def evaluate_freshness(job: NormalizedJob, now: datetime | None = None,
                       max_days: int = 14) -> FreshnessResult:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if job.posted_at is None:
        window = (job.provenance or {}).get(SOURCE_WINDOW_KEY)
        if window and window <= max_days * 24:
            return FreshnessResult(True, None, "UNDATED_WITHIN_SOURCE_WINDOW")
        return FreshnessResult(False, None, "MISSING_POSTING_DATE")

    posted = job.posted_at
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)

    if job.date_precision == "DAY":
        age = float((now.date() - posted.astimezone(timezone.utc).date()).days)
        if age < 0:
            return FreshnessResult(False, age, "FUTURE_POSTING_DATE")
    else:
        age = (now - posted).total_seconds() / 86400.0
        if age < -_CLOCK_SKEW_DAYS:
            return FreshnessResult(False, age, "FUTURE_POSTING_DATE")
        age = max(age, 0.0)

    if age > max_days:
        return FreshnessResult(False, age, "STALE")
    return FreshnessResult(True, age, None)


class FreshnessFilter:
    def __init__(self, settings: Settings | None = None, logger=None) -> None:
        self.settings = settings or get_settings()
        self.log = logger or get_logger("nexbase.pipeline.freshness")

    def evaluate(self, job: NormalizedJob, now: datetime | None = None) -> FreshnessResult:
        return evaluate_freshness(job, now=now, max_days=self.settings.freshness_max_days)

    def split(self, jobs: list[NormalizedJob], now: datetime | None = None):
        """``(fresh, rejected)``, each a list of ``(job, FreshnessResult)``."""
        fresh, rejected = [], []
        reasons: dict[str, int] = {}
        for job in jobs:
            result = self.evaluate(job, now=now)
            (fresh if result.keep else rejected).append((job, result))
            if result.reason:
                reasons[result.reason] = reasons.get(result.reason, 0) + 1
        self.log.info("freshness_complete", jobs_in=len(jobs), kept=len(fresh),
                      rejected=len(rejected), reasons=reasons)
        return fresh, rejected
