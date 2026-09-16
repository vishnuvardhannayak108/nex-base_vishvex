"""Module 4: Deduplication.

Brief: *"Deduplicate by normalized company name and domain."*

A naive ``(domain, name)`` key fails across sources, because the same employer
arrives with a domain from Indeed's ``company_url_direct``, no domain at all
from ZipRecruiter, and no domain from an ATS row. Keying on the pair alone
would emit three companies.

So deduplication runs in two passes:

1. **Bucket by normalized name.** Names are already legal-suffix-stripped, so
   "Acme Manufacturing Inc." and "Acme Manufacturing LLC" collide here.
2. **Split each bucket by domain**, but treat domain-less sightings as
   *compatible* with a domained one in the same name bucket, and fold them in.
   Two different domains under one name stay separate - that is a genuine
   name collision between distinct companies (e.g. two "Summit Construction").

Repeat jobs for the same company become additional hiring signals
(``hiring_intensity``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from nexbase.logging_setup import get_logger
from nexbase.pipeline.normalize import NormalizedJob

#: US state abbreviations, used to corroborate a domainless company.
_STATE_RE = re.compile(r"\b([a-z]{2})\b\s*\d{0,5}\s*$")


def location_bucket(location_normalized: str | None) -> str:
    """A coarse geography key for a domainless sighting.

    Two employers can share a normalized name; without a domain the only thing
    separating them is where they are hiring. The state is used rather than the
    city so that one company advertising across a metro still merges.
    """
    if not location_normalized:
        return ""
    text = location_normalized.strip().lower()
    match = _STATE_RE.search(text)
    if match:
        return match.group(1)
    parts = [p.strip() for p in text.split(",") if p.strip()]
    return parts[-1] if parts else text


#: Domain evidence strength, strongest first. Used to pick a bucket's domain.
_DOMAIN_SOURCE_RANK = {
    "COMPANY_WEBSITE": 0,
    "COMPANY_URL": 1,
    "APPLICATION_URL": 2,
    "APPLY_URL": 3,
    "OBSERVED_EMAIL": 4,
    "NONE": 9,
}


@dataclass
class CompanyAggregate:
    company_name: str | None
    company_name_normalized: str
    domain: str
    jobs: list[NormalizedJob] = field(default_factory=list)
    merged_sources: set[str] = field(default_factory=set)

    @property
    def hiring_intensity(self) -> int:
        return len(self.jobs)

    @property
    def dedup_key(self) -> tuple[str, str]:
        return (
            self.domain or "NO_DOMAIN",
            self.company_name_normalized or "NO_NAME",
        )

    @property
    def company_website(self) -> str | None:
        """Best employer URL observed across every sighting."""
        for job in sorted(
            self.jobs, key=lambda j: _DOMAIN_SOURCE_RANK.get(j.domain_source, 9)
        ):
            if job.company_website:
                return job.company_website
        if self.domain:
            return f"https://{self.domain}"
        return None

    @property
    def location(self) -> str | None:
        for job in self.jobs:
            if job.location:
                return job.location
        return None

    @property
    def observed_industry(self) -> str | None:
        for job in self.jobs:
            if job.company_industry:
                return job.company_industry
        return None

    @property
    def search_industry(self) -> str | None:
        """Industry of the query that found this company - intent, not evidence."""
        for job in self.jobs:
            if job.search_industry:
                return job.search_industry
        return None

    @property
    def observed_employee_count(self) -> str | None:
        for job in self.jobs:
            if job.company_employee_count:
                return job.company_employee_count
        return None

    @property
    def min_applicant_count(self) -> int | None:
        counts = [j.applicant_count for j in self.jobs if j.applicant_count is not None]
        return min(counts) if counts else None

    @property
    def observed_emails(self) -> list[str]:
        seen: dict[str, None] = {}
        for job in self.jobs:
            for email in job.observed_emails:
                seen.setdefault(email.strip().lower(), None)
        return list(seen)

    @property
    def source_sites(self) -> list[str]:
        return sorted({j.source_site for j in self.jobs if j.source_site})

    @property
    def best_source_priority(self) -> int:
        return min((j.source_priority for j in self.jobs), default=3)


def _best_domain(jobs: list[NormalizedJob]) -> str:
    """Pick the strongest-evidence domain in a name bucket."""
    domained = [j for j in jobs if j.domain]
    if not domained:
        return ""
    domained.sort(key=lambda j: (_DOMAIN_SOURCE_RANK.get(j.domain_source, 9), j.domain))
    return domained[0].domain


def dedupe_jobs(jobs: list[NormalizedJob]) -> list[CompanyAggregate]:
    """Group normalized jobs into companies, merging across sources."""
    # Pass 1: bucket by normalized name.
    by_name: dict[str, list[NormalizedJob]] = {}
    for job in jobs:
        by_name.setdefault(job.company_name_normalized or "NO_NAME", []).append(job)

    aggregates: dict[tuple[str, str], CompanyAggregate] = {}

    for name, bucket in by_name.items():
        distinct_domains = {j.domain for j in bucket if j.domain}

        if len(distinct_domains) <= 1:
            domain = _best_domain(bucket)
            if domain:
                # A domain identifies the employer, so the whole bucket is one
                # company and domain-less sightings inherit it.
                key = (domain, name)
                agg = aggregates.setdefault(
                    key,
                    CompanyAggregate(
                        company_name=_display_name(bucket),
                        company_name_normalized=name,
                        domain=domain,
                    ),
                )
                agg.jobs.extend(bucket)
                agg.merged_sources.update(j.source_site for j in bucket if j.source_site)
                continue

            # No domain anywhere in the bucket. Merging on the name alone put
            # three unrelated "Summit Construction" employers in Colorado,
            # Florida and Idaho into one record, so geography has to corroborate
            # the name before they are treated as the same company.
            for geo, subset in _by_location(bucket).items():
                key = ("NO_DOMAIN", f"{name}|{geo}" if geo else name)
                agg = aggregates.setdefault(
                    key,
                    CompanyAggregate(
                        company_name=_display_name(subset),
                        company_name_normalized=name,
                        domain="",
                    ),
                )
                agg.jobs.extend(subset)
                agg.merged_sources.update(j.source_site for j in subset if j.source_site)
            continue

        # Genuine name collision across different domains: keep them apart,
        # and attach each domain-less sighting to nothing (it cannot be
        # attributed to one of them without guessing).
        for domain in sorted(distinct_domains):
            subset = [j for j in bucket if j.domain == domain]
            key = (domain, name)
            agg = aggregates.setdefault(
                key,
                CompanyAggregate(
                    company_name=_display_name(subset),
                    company_name_normalized=name,
                    domain=domain,
                ),
            )
            agg.jobs.extend(subset)
            agg.merged_sources.update(j.source_site for j in subset if j.source_site)

        orphans = [j for j in bucket if not j.domain]
        for geo, subset in _by_location(orphans).items():
            key = ("NO_DOMAIN", f"{name}|{geo}" if geo else name)
            agg = aggregates.setdefault(
                key,
                CompanyAggregate(
                    company_name=_display_name(subset),
                    company_name_normalized=name,
                    domain="",
                ),
            )
            agg.jobs.extend(subset)
            agg.merged_sources.update(j.source_site for j in subset if j.source_site)

    # Within each company, collapse the same posting seen on multiple boards.
    for agg in aggregates.values():
        agg.jobs = _dedupe_postings(agg.jobs)

    return sorted(aggregates.values(), key=lambda c: c.hiring_intensity, reverse=True)


def _by_location(jobs: list[NormalizedJob]) -> dict[str, list[NormalizedJob]]:
    """Split domainless sightings by coarse geography, preserving order."""
    buckets: dict[str, list[NormalizedJob]] = {}
    for job in jobs:
        buckets.setdefault(location_bucket(job.location_normalized), []).append(job)
    return buckets


def _display_name(jobs: list[NormalizedJob]) -> str | None:
    """Prefer the longest raw name; it usually carries the fullest legal form."""
    names = [j.company_name for j in jobs if j.company_name]
    return max(names, key=len) if names else None


def _dedupe_postings(jobs: list[NormalizedJob]) -> list[NormalizedJob]:
    """Collapse the same role cross-posted to several boards.

    Grouped on normalized title + normalized location, then resolved by
    requisition id. Keying on title and location alone collapsed three genuinely
    distinct requisitions into one and understated hiring intensity, but keying
    on the id alone would stop the same job cross-posted to three boards from
    collapsing at all - ids are only comparable within one source.

    So: count distinct ids per source, and let the source that can see the most
    requisitions decide how many there are. Ties go to the strongest source (ATS
    beats job board) and, within that, to the copy carrying a posting date.
    """
    buckets: dict[tuple[str, str], list[NormalizedJob]] = {}
    for job in jobs:
        buckets.setdefault((job.title_normalized, job.location_normalized), []).append(job)

    out: list[NormalizedJob] = []
    for bucket in buckets.values():
        cross_posted = sorted({j.source_site for j in bucket if j.source_site})

        by_site: dict[str, dict[str, list[NormalizedJob]]] = {}
        for job in bucket:
            site = by_site.setdefault(job.source_site or "", {})
            site.setdefault(job.external_id or "", []).append(job)

        # The source reporting the most distinct requisitions is believed.
        best_site = min(
            by_site,
            key=lambda site: (
                -len(by_site[site]),
                min(j.source_priority for reqs in by_site[site].values() for j in reqs),
                site,
            ),
        )

        for requisition in by_site[best_site].values():
            requisition.sort(
                key=lambda j: (
                    j.source_priority,
                    0 if j.posted_at else 1,
                    0 if j.description else 1,
                )
            )
            winner = requisition[0]
            if len(cross_posted) > 1:
                winner.raw = dict(winner.raw)
                winner.raw["cross_posted_on"] = cross_posted
            out.append(winner)
    return out


class Deduplicator:
    """Deduplicates normalized jobs into company aggregates."""

    def __init__(self, logger=None) -> None:
        self.log = logger or get_logger("nexbase.pipeline.dedupe")

    def dedupe(self, jobs: list[NormalizedJob]) -> list[CompanyAggregate]:
        aggregates = dedupe_jobs(jobs)
        multi_source = sum(1 for a in aggregates if len(a.merged_sources) > 1)
        self.log.info(
            "dedupe_complete",
            input_jobs=len(jobs),
            companies=len(aggregates),
            multi_source_companies=multi_source,
            deduped_jobs=sum(a.hiring_intensity for a in aggregates),
        )
        return aggregates
