"""Company identification and company deduplication.

Runs on fresh, deduplicated postings and groups them into companies. Identity
evidence is ranked, strongest first:

1. **DOMAIN** - the employer website a source published (``company_website``,
   or a non-platform ``company_url``). Same domain, same company.
2. **EMPLOYER_URL** - a domain seen only in an apply URL on the employer's own
   host, or an employer email. Weaker, because a careers host can be shared, so
   it only joins postings whose normalized employer names also agree.
3. **SOURCE_ID** - the source's own company identifier: an Indeed ``/cmp/``
   page, a LinkedIn ``/company/`` page, an ATS tenant (greenhouse, lever, ...).
4. **NAME_LOCATION** - normalized employer name plus US state.

A posting with none of these (a name but no domain, source id or state) is
**NAME_ONLY** and never merged on the name alone. When another company shares
its name, it is marked ``identity_ambiguous`` so qualification sends it to review.

Weaker evidence never overrides stronger: a source id or a name+state match that
would join two companies with *different* domains joins nothing; only the
domainless postings in that group are merged with each other.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import unquote

from nexbase.logging_setup import get_logger
from nexbase.pipeline.normalize import NormalizedJob

#: Domain evidence strength, strongest first.
_DOMAIN_SOURCE_RANK = {
    "COMPANY_WEBSITE": 0, "COMPANY_URL": 1, "APPLICATION_URL": 2, "APPLY_URL": 3,
    "OBSERVED_EMAIL": 4, "NONE": 9,
}
_WEBSITE_EVIDENCE = frozenset({"COMPANY_WEBSITE", "COMPANY_URL"})

#: Company identifiers in source URLs, verified against live rows on 2026-09-16.
_SOURCE_ID_PATTERNS = tuple((family, re.compile(pattern, re.IGNORECASE)) for family, pattern in (
    ("indeed", r"(?:^|[/.])indeed\.com/cmp/([^/?#]+)"),
    ("linkedin", r"(?:^|[/.])linkedin\.com/company/([^/?#]+)"),
    ("greenhouse", r"(?:boards|job-boards)\.greenhouse\.io/([^/?#]+)"),
    ("lever", r"jobs\.lever\.co/([^/?#]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([^/?#]+)"),
    ("smartrecruiters", r"jobs\.smartrecruiters\.com/([^/?#]+)"),
    ("workable", r"apply\.workable\.com/(?!j/)([^/?#]+)"),
    ("bamboohr", r"//([a-z0-9-]+)\.bamboohr\.com"),
    ("breezy", r"//([a-z0-9-]+)\.breezy\.hr"),
    ("jazzhr", r"//([a-z0-9-]+)\.applytojob\.com"),
    ("recruitee", r"//([a-z0-9-]+)\.recruitee\.com"),
))


def source_ids(job: NormalizedJob) -> set[str]:
    """``family:identifier`` strings for every source company id on a posting."""
    found: set[str] = set()
    for url in (job.company_url, job.application_url, job.apply_url):
        for family, pattern in _SOURCE_ID_PATTERNS:
            match = pattern.search(url or "")
            if match:
                found.add(f"{family}:{unquote(match.group(1)).strip().lower()}")
    # Paylocity URLs carry no tenant; its ats_id is "<board guid>:<job id>".
    ats_id = str((job.raw or {}).get("ats_id") or "")
    if job.ats_platform == "paylocity" and ":" in ats_id:
        found.add(f"paylocity:{ats_id.split(':', 1)[0].lower()}")
    return found


@dataclass
class CompanyAggregate:
    company_name: str | None
    company_name_normalized: str
    domain: str
    jobs: list[NormalizedJob] = field(default_factory=list)
    #: DOMAIN | EMPLOYER_URL | SOURCE_ID | NAME_LOCATION | NAME_ONLY
    identity_basis: str = "NAME_LOCATION"
    #: The evidence the company is keyed on, e.g. "domain:acme.com".
    identity_key: str = ""
    source_ids: list[str] = field(default_factory=list)
    #: NAME_ONLY and another company has the same name: it may be that company.
    identity_ambiguous: bool = False

    @property
    def merged_sources(self) -> set[str]:
        return {j.source_site for j in self.jobs if j.source_site}

    @property
    def hiring_intensity(self) -> int:
        return len(self.jobs)

    @property
    def dedup_key(self) -> tuple[str, str]:
        """Unique per company: ``(domain, name)``, or ``("NO_DOMAIN", identity_key)``."""
        if self.domain:
            return (self.domain, self.company_name_normalized or "NO_NAME")
        return ("NO_DOMAIN", self.identity_key or self.company_name_normalized or "NO_NAME")

    @property
    def company_website(self) -> str | None:
        """Best employer URL observed across every sighting."""
        for job in sorted(self.jobs, key=lambda j: _DOMAIN_SOURCE_RANK.get(j.domain_source, 9)):
            if job.company_website:
                return job.company_website
        return f"https://{self.domain}" if self.domain else None

    @property
    def location(self) -> str | None:
        return next((j.location for j in self.jobs if j.location), None)

    @property
    def states(self) -> list[str]:
        return sorted({j.state for j in self.jobs if j.state})

    @property
    def observed_industry(self) -> str | None:
        return next((j.company_industry for j in self.jobs if j.company_industry), None)

    @property
    def search_industry(self) -> str | None:
        """Industry of the query that found this company - intent, not evidence."""
        return next((j.search_industry for j in self.jobs if j.search_industry), None)

    @property
    def observed_employee_count(self) -> str | None:
        return next((j.company_employee_count for j in self.jobs if j.company_employee_count), None)

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
        return sorted(self.merged_sources)

    @property
    def best_source_priority(self) -> int:
        return min((j.source_priority for j in self.jobs), default=3)


@dataclass
class FreshCompany:
    """A company built from fresh postings, with each posting's freshness verdict."""

    company: CompanyAggregate
    fresh_jobs: list[tuple[NormalizedJob, object]]

    @property
    def hiring_intensity(self) -> int:
        return len(self.fresh_jobs)

    @property
    def min_age_days(self) -> float | None:
        ages = [r.age_days for _, r in self.fresh_jobs if r.age_days is not None]
        return min(ages) if ages else None


# ---------------------------------------------------------------------------
class _Components:
    """Union-find over postings that refuses to join two different domains."""

    def __init__(self, jobs: list[NormalizedJob]) -> None:
        self.parent = list(range(len(jobs)))
        self.domain = {i: job.domain or "" for i, job in enumerate(jobs)}

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra
            self.domain[ra] = self.domain[ra] or self.domain[rb]

    def union_if_consistent(self, members: list[int]) -> bool:
        """Join ``members`` unless their components carry different domains.

        On a conflict only the domainless components are joined with each
        other; returns False so the caller can count the conflict.
        """
        roots = sorted({self.find(i) for i in members})
        domains = {self.domain[r] for r in roots if self.domain[r]}
        if len(domains) > 1:
            domainless = [r for r in roots if not self.domain[r]]
            for other in domainless[1:]:
                self.union(domainless[0], other)
            return False
        for other in roots[1:]:
            self.union(roots[0], other)
        return True


def identify_companies(fresh: list[tuple[NormalizedJob, object]]) -> tuple[list[FreshCompany], dict]:
    """Group fresh postings into companies. Returns ``(companies, stats)``."""
    jobs = [job for job, _ in fresh]
    components = _Components(jobs)
    stats = {"conflicts": 0}

    def groups(keys_for) -> list[list[int]]:
        grouped: dict[tuple, list[int]] = {}
        for i, job in enumerate(jobs):
            for key in keys_for(job):
                grouped.setdefault(key, []).append(i)
        return [members for _, members in sorted(grouped.items()) if len(members) > 1]

    # 1 + 2. Domains: a website domain alone, an employer-URL domain with the name.
    for members in groups(lambda j: (
            [("domain", j.domain)] if j.domain and j.domain_source in _WEBSITE_EVIDENCE else []
    ) + ([("domain_name", j.domain, j.company_name_normalized)] if j.domain else [])):
        for other in members[1:]:
            components.union(members[0], other)
    # 3. Source company ids.
    for members in groups(lambda j: [("source", s) for s in sorted(source_ids(j))]):
        stats["conflicts"] += not components.union_if_consistent(members)
    # 4. Name + state. A name alone joins nothing.
    for members in groups(lambda j: [("name", j.company_name_normalized, j.state)] if j.state else []):
        stats["conflicts"] += not components.union_if_consistent(members)

    by_root: dict[int, list[int]] = {}
    for i in range(len(jobs)):
        by_root.setdefault(components.find(i), []).append(i)

    companies = [_company([fresh[i] for i in members]) for members in by_root.values()]
    names = Counter(c.company.company_name_normalized for c in companies)
    for c in companies:
        c.company.identity_ambiguous = (c.company.identity_basis == "NAME_ONLY"
                                        and names[c.company.company_name_normalized] > 1)
    companies.sort(key=lambda c: (-c.hiring_intensity, c.company.dedup_key))
    stats["basis"] = dict(Counter(c.company.identity_basis for c in companies))
    stats["ambiguous"] = sum(c.company.identity_ambiguous for c in companies)
    return companies, stats


def _company(pairs: list[tuple[NormalizedJob, object]]) -> FreshCompany:
    jobs = [job for job, _ in pairs]
    names = Counter(j.company_name_normalized for j in jobs)
    name = sorted(names, key=lambda n: (-names[n], -len(n), n))[0]
    domained = sorted((j for j in jobs if j.domain),
                      key=lambda j: _DOMAIN_SOURCE_RANK.get(j.domain_source, 9))
    domain = domained[0].domain if domained else ""
    ids = sorted({sid for j in jobs for sid in source_ids(j)})

    if domain:
        basis = "DOMAIN" if domained[0].domain_source in _WEBSITE_EVIDENCE else "EMPLOYER_URL"
        key = f"domain:{domain}"
    elif ids:
        basis, key = "SOURCE_ID", f"source:{ids[0]}"
    elif any(j.state for j in jobs):
        state = Counter(j.state for j in jobs if j.state).most_common(1)[0][0].lower()
        basis, key = "NAME_LOCATION", f"name:{name}|{state}"
    else:
        # A single posting (nothing merged it): keyed on the posting itself.
        job = jobs[0]
        basis = "NAME_ONLY"
        key = f"name:{name}|posting:{job.source_site}:{job.external_id or job.evidence_url}"

    raw_names = [j.company_name for j in jobs if j.company_name]
    company = CompanyAggregate(
        company_name=max(raw_names, key=len) if raw_names else None,
        company_name_normalized=name, domain=domain, jobs=jobs,
        identity_basis=basis, identity_key=key, source_ids=ids,
    )
    return FreshCompany(company, pairs)


class CompanyIdentifier:
    def __init__(self, logger=None) -> None:
        self.log = logger or get_logger("nexbase.pipeline.company_identity")

    def identify(self, fresh: list[tuple[NormalizedJob, object]]) -> list[FreshCompany]:
        companies, stats = identify_companies(fresh)
        self.log.info("company_identification_complete", jobs=len(fresh),
                      companies=len(companies), identity_basis=stats["basis"],
                      domain_conflicts=stats["conflicts"],
                      ambiguous_identities=stats["ambiguous"])
        return companies


def prepare_companies(raw_jobs, settings=None, now: datetime | None = None, logger=None):
    """Normalize -> job dedup -> freshness -> company identification, in plan order.

    Returns ``(companies, discarded, duplicates, stale)``. The runner runs the
    same stages one by one so each is timed and recorded.
    """
    from nexbase.pipeline.dedupe import JobDeduplicator
    from nexbase.pipeline.freshness import FreshnessFilter
    from nexbase.pipeline.normalize import Normalizer

    normalized, discarded = Normalizer(logger).normalize(raw_jobs)
    kept, duplicates = JobDeduplicator(logger).dedupe(normalized)
    fresh, stale = FreshnessFilter(settings, logger).split(kept, now=now)
    return CompanyIdentifier(logger).identify(fresh), discarded, duplicates, stale
