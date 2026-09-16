"""Job deduplication: one posting seen several times becomes one job.

Runs on normalized postings, before freshness and before company
identification. Deliberately conservative - two postings are only the same job
when the evidence says so:

1. **Same portal, same id.** Overlapping queries (nationwide then state) return
   the same row twice.
2. **Cross-posted.** Same normalized employer name, same normalized title and
   the same US location (at least the state). Postings with no state, or whose
   employer domains disagree, are never merged across portals. Within such a
   group, the portal that shows the most distinct requisition ids decides how
   many openings there are, so three genuine "Machinist, Toledo, OH" openings
   are not collapsed into one.

The kept posting prefers a posting date, then first-party (ATS) data, then a
description. Every merged copy is recorded on the kept posting's ``raw`` for
audit (``duplicates``, ``cross_posted_on``).
"""
from __future__ import annotations

from dataclasses import dataclass

from nexbase.logging_setup import get_logger
from nexbase.pipeline.normalize import NormalizedJob


@dataclass
class DuplicateJob:
    job: NormalizedJob
    kept: NormalizedJob
    reason: str  # SAME_SOURCE_ID | CROSS_POSTED


def _preference(job: NormalizedJob) -> tuple:
    return (0 if job.posted_at else 1, job.source_priority, 0 if job.description else 1)


def _reference(job: NormalizedJob) -> dict:
    return {"source_site": job.source_site, "external_id": job.external_id,
            "url": job.evidence_url,
            "posted_at": job.posted_at.isoformat() if job.posted_at else None}


def _record(kept: NormalizedJob, dropped: list[NormalizedJob], reason: str,
            duplicates: list[DuplicateJob]) -> None:
    if not dropped:
        return
    kept.raw = dict(kept.raw or {})
    kept.raw.setdefault("duplicates", []).extend(
        {**_reference(job), "reason": reason} for job in dropped)
    duplicates.extend(DuplicateJob(job, kept, reason) for job in dropped)


def dedupe_postings(jobs: list[NormalizedJob]) -> tuple[list[NormalizedJob], list[DuplicateJob]]:
    """Return ``(kept, duplicates)``, preserving the input order of kept jobs."""
    duplicates: list[DuplicateJob] = []

    # 1. Same portal, same id.
    groups: dict[tuple, list[NormalizedJob]] = {}
    for job in jobs:
        key = ((job.source_site, job.external_id) if job.source_site and job.external_id
               else ("__row__", id(job)))
        groups.setdefault(key, []).append(job)
    unique: list[NormalizedJob] = []
    for group in groups.values():
        winner = min(group, key=_preference)
        _record(winner, [j for j in group if j is not winner], "SAME_SOURCE_ID", duplicates)
        unique.append(winner)

    # 2. Cross-posted: same employer name, title and state-level location.
    buckets: dict[tuple, list[NormalizedJob]] = {}
    for job in unique:
        if job.state and job.title_normalized:
            key = (job.company_name_normalized, job.title_normalized, job.location_normalized)
        else:
            key = ("__unmergeable__", id(job))
        buckets.setdefault(key, []).append(job)

    kept_ids: set[int] = set()
    for bucket in buckets.values():
        for group in _split_by_domain(bucket):
            kept_ids.update(id(j) for j in _resolve_cross_posts(group, duplicates))

    kept = [job for job in unique if id(job) in kept_ids]
    return kept, duplicates


def _split_by_domain(bucket: list[NormalizedJob]) -> list[list[NormalizedJob]]:
    """Two different employer domains are two employers, whatever the name says."""
    domains = {j.domain for j in bucket if j.domain}
    if len(domains) <= 1:
        return [bucket]
    groups = [[j for j in bucket if j.domain == d] for d in sorted(domains)]
    domainless = [j for j in bucket if not j.domain]
    return groups + ([domainless] if domainless else [])


def _resolve_cross_posts(group: list[NormalizedJob], duplicates: list[DuplicateJob]) -> list[NormalizedJob]:
    if len(group) == 1:
        return group
    sites = sorted({j.source_site for j in group if j.source_site})
    by_site: dict[str, dict[str, list[NormalizedJob]]] = {}
    for job in group:
        by_site.setdefault(job.source_site or "", {}).setdefault(job.external_id or "", []).append(job)

    # The portal that can see the most distinct requisitions is believed.
    best_site = min(by_site, key=lambda site: (
        -len(by_site[site]),
        min(j.source_priority for reqs in by_site[site].values() for j in reqs),
        site))
    winners = [min(reqs, key=_preference) for reqs in by_site[best_site].values()]
    losers = [j for j in group if all(j is not w for w in winners)]

    for winner in winners:
        if len(sites) > 1:
            winner.raw = dict(winner.raw or {})
            winner.raw["cross_posted_on"] = sites
    # With several openings a copy cannot be matched to one of them without
    # guessing; copies are recorded against the group's first opening.
    _record(winners[0], losers, "CROSS_POSTED", duplicates)
    return winners


class JobDeduplicator:
    def __init__(self, logger=None) -> None:
        self.log = logger or get_logger("nexbase.pipeline.dedupe")

    def dedupe(self, jobs: list[NormalizedJob]) -> tuple[list[NormalizedJob], list[DuplicateJob]]:
        kept, duplicates = dedupe_postings(jobs)
        reasons: dict[str, int] = {}
        for duplicate in duplicates:
            reasons[duplicate.reason] = reasons.get(duplicate.reason, 0) + 1
        self.log.info("job_dedupe_complete", input_jobs=len(jobs), kept=len(kept),
                      duplicates=reasons)
        return kept, duplicates
