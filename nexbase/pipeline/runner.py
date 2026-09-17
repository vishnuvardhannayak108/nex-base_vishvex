"""Pipeline runner: orchestrates every module in the locked order.

Single entry point used by the API, the CLI and tests:

    1 Discover jobs: a USA-wide plan run through the source registry
    2 Normalize (and screen out postings that cannot become leads)
    3 Deduplicate jobs
    4 Freshness filter (<= 14 days)
    5 Identify and deduplicate companies
    6 Qualify (direct employer, size, industry, hiring signals) and resolve
      official domains
    7 Persist every company, its jobs and its qualification reasons
    8 Free / public contact discovery + email classification + POC ranking,
      QUALIFIED companies only
    9 Enrichment waterfall, QUALIFIED companies only: ZoomInfo PRIMARY, Apollo
      and Apify FALLBACK only when ZoomInfo could not supply what was required

Apollo, Apify and email verification are not executed here. Nothing here sends
email.

One :class:`AccessLayer` is shared for the whole run, so the rate limiter and
the Camoufox budget are genuinely global rather than resetting per company.
"""
from __future__ import annotations

import time as _time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from nexbase.access.fetcher import AccessLayer
from nexbase.config import Settings, get_settings
from nexbase.contacts.discovery import ContactDiscovery
from nexbase.contacts.ranking import ContactRanker
from nexbase.core.enums import DiscoveryStage, EmailStatus, QualificationStatus, SourcePriority
from nexbase.core.models import RawJob
from nexbase.db.repository import InertRepository, SupabaseRepository
from nexbase.discovery.linkedin_signal import LinkedInApplicantEnricher
from nexbase.discovery.planner import DiscoveryPlan
from nexbase.discovery.registry import SourceRegistry, build_registry
from nexbase.email.discovery import EmailDiscovery
from nexbase.enrichment.apify import ApifyAdapter
from nexbase.enrichment.apollo import ApolloAdapter
from nexbase.enrichment.base import CompanyContext, FallbackProvider
from nexbase.enrichment.waterfall import EnrichmentWaterfall
from nexbase.enrichment.zoominfo_stage import ZoomInfoAdapter
from nexbase.logging_setup import bind_run, ensure_logging_configured, get_logger, stage
from nexbase.pipeline.company_identity import CompanyIdentifier, FreshCompany
from nexbase.pipeline.dedupe import JobDeduplicator
from nexbase.pipeline.freshness import FreshnessFilter
from nexbase.pipeline.normalize import NormalizedJob, Normalizer
from nexbase.pipeline.profile import CompanyProfile, build_profile
from nexbase.pipeline.qualification import QualificationGate, QualificationResult
from nexbase.pipeline.domain_resolver import (
    ACCEPTABLE,
    DomainCache,
    DomainResolver,
    NullDomainResolver,
    domain_row,
    negative_domain_row,
)
from nexbase.pipeline.size_resolver import (
    NullSizeResolver,
    SizeCache,
    SizeResolver,
    WebSizeResolver,
)


def _json_safe(value: Any) -> Any:
    """Recursively convert values into JSON-serializable primitives."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    try:
        return str(value)
    except Exception:
        return None


@dataclass
class QualifiedLead:
    company_name: str
    display_name: str | None
    domain: str
    website: str | None
    location: str | None
    status: str
    score: float
    job_count: int
    freshest_job_age_days: float | None
    client_industry: str | None
    industry_source: str | None
    employee_size: str | None
    employee_size_source: str | None
    applicant_count: int | None
    review_flags: list[str] = field(default_factory=list)
    contacts: list[dict] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    #: PERSONAL_EMAIL_FOUND | ROLE_EMAIL_FOUND | MULTIPLE_EMAILS_FOUND |
    #: NO_PUBLIC_EMAIL_FOUND. A lead with only a role mailbox is still a lead.
    email_status: str = EmailStatus.NO_PUBLIC_EMAIL_FOUND.value
    #: Every observed address with its source URL, portal and stage.
    observed_emails: list[dict] = field(default_factory=list)
    #: Addresses attributable to a named person.
    named_contact_emails: list[dict] = field(default_factory=list)
    #: Shared mailboxes: company-level evidence, never a person.
    role_mailboxes: list[str] = field(default_factory=list)
    #: Portals actually searched for this employer.
    portals_searched: list[str] = field(default_factory=list)
    #: Which contact-discovery stages ran.
    discovery_stages: list[str] = field(default_factory=list)
    domain_confidence: str | None = None
    domain_source: str | None = None
    #: NOT_RUN | COMPLETED | PAGE_BUDGET_EXHAUSTED | SKIPPED_COMPANY_BUDGET |
    #: FAILED. Only QUALIFIED companies are ever searched.
    contact_discovery: str = "NOT_RUN"
    #: The enrichment waterfall for this lead: every provider attempt (called or
    #: not, status, fallback reason, match method, fields supplied, credits).
    enrichment: dict = field(default_factory=dict)
    rejection_reasons: list[str] = field(default_factory=list)
    evidence_url: str | None = None
    source_sites: list[str] = field(default_factory=list)


@dataclass
class RejectedLead:
    company_name: str
    domain: str
    stage: str
    reasons: list[str]


@dataclass
class PipelineReport:
    run_key: str | None = None
    raw_jobs: int = 0
    normalized_jobs: int = 0
    duplicate_jobs: int = 0
    fresh_jobs: int = 0
    companies: int = 0
    #: "STAGE:REASON" -> postings dropped before company identification.
    discarded: dict = field(default_factory=dict)
    qualified: list[QualifiedLead] = field(default_factory=list)
    needs_review: list[QualifiedLead] = field(default_factory=list)
    rejected: list[RejectedLead] = field(default_factory=list)
    qualification_records: list[dict] = field(default_factory=list)
    #: Per-stage status and duration, from ``logging_setup.stage``.
    stages: dict = field(default_factory=dict)
    size_resolution: dict = field(default_factory=dict)
    domain_resolution: dict = field(default_factory=dict)
    contact_discovery: dict = field(default_factory=dict)
    #: Provider -> attempt status counts, and credits spent per provider.
    enrichment: dict = field(default_factory=dict)
    #: Per-source outcome, so an empty or broken source stays visible
    #: instead of being folded into a single total.
    source_status: dict = field(default_factory=dict)
    #: Why each (source, query) stopped: exhausted, blocked, robots, rate
    #: limited, capped by the source, or capped by our own budget.
    coverage: dict = field(default_factory=dict)
    access: dict = field(default_factory=dict)
    pitfalls: list[dict] = field(default_factory=list)
    plan: dict | None = None
    raw_job_records: list[dict] = field(default_factory=list)
    normalized_job_records: list[dict] = field(default_factory=list)

    def append_pitfall(self, **kwargs) -> None:
        self.pitfalls.append(kwargs)

    def to_dict(self, include_records: bool = False) -> dict:
        payload = {
            "meta": {
                "run_key": self.run_key,
                "raw_jobs": self.raw_jobs,
                "normalized_jobs": self.normalized_jobs,
                "duplicate_jobs": self.duplicate_jobs,
                "fresh_jobs": self.fresh_jobs,
                "companies": self.companies,
                "qualified": len(self.qualified),
                "needs_review": len(self.needs_review),
                "rejected": len(self.rejected),
            },
            "stages": self.stages,
            "discarded": self.discarded,
            "size_resolution": self.size_resolution,
            "domain_resolution": self.domain_resolution,
            "contact_discovery": self.contact_discovery,
            "enrichment": self.enrichment,
            "source_status": self.source_status,
            "coverage": self.coverage,
            "qualified_leads": [asdict(lead) for lead in self.qualified],
            "needs_review_leads": [asdict(lead) for lead in self.needs_review],
            "rejected_leads": [asdict(lead) for lead in self.rejected],
            "qualification": self.qualification_records,
            "access": self.access,
            "pitfalls": self.pitfalls,
            "plan": self.plan,
        }
        if include_records:
            payload["raw_jobs_detail"] = self.raw_job_records
            payload["normalized_jobs_detail"] = self.normalized_job_records
        return _json_safe(payload)


class PipelineRunner:
    """End-to-end orchestration of the NexBase pipeline."""

    def __init__(
        self,
        settings: Settings | None = None,
        repo: SupabaseRepository | None = None,
        logger=None,
        access: AccessLayer | None = None,
        size_resolver: SizeResolver | None = None,
        domain_resolver=None,
        registry: SourceRegistry | None = None,
        enrichment_providers: list[FallbackProvider] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        #: Priority order, primary first. Default: ZoomInfo, Apollo, Apify.
        self._enrichment_providers = enrichment_providers
        ensure_logging_configured(self.settings.log_level)
        self.log = logger or get_logger("nexbase.pipeline.runner")
        self.repo = repo if repo is not None else SupabaseRepository(settings=self.settings)
        self.access = access or AccessLayer(settings=self.settings, repo=self.repo)
        self._size_cache = SizeCache(
            repo=self.repo, max_age_days=self.settings.size_cache_max_age_days
        )
        if size_resolver is not None:
            self._size_resolver: SizeResolver = size_resolver
        elif self.settings.size_resolver_enabled:
            self._size_resolver = WebSizeResolver(
                access=self.access, logger=self.log,
                max_pages=self.settings.size_resolver_max_pages,
            )
        else:
            self._size_resolver = NullSizeResolver()

        if domain_resolver is not None:
            self._domain_resolver = domain_resolver
        elif self.settings.domain_resolver_enabled:
            self._domain_resolver = DomainResolver(
                access=self.access, settings=self.settings, logger=self.log
            )
        else:
            self._domain_resolver = NullDomainResolver()
        self._domain_cache = DomainCache(
            repo=self.repo,
            max_age_days=self.settings.domain_cache_max_age_days,
            negative_max_age_days=self.settings.domain_negative_cache_days,
        )
        self._domain_failures: set[tuple[str, str]] = set()
        self._domain_confidence: dict[tuple[str, str], str] = {}
        self._domain_results: dict[tuple[str, str], object] = {}
        self._domain_searches = 0
        #: Candidates too weak to attach, kept for a human decision.
        self._domain_review: dict[tuple[str, str], dict] = {}
        #: Built per run when not injected, so run-scoped state (the ATS slice
        #: cache) never leaks between runs.
        self._registry = registry

    def _audit(self, event: str, level: str = "INFO", **data) -> None:
        self.repo.insert_audit(
            {"event": event, "data": _json_safe(data), "level": level}
        )

    # ------------------------------------------------------------------
    def run(
        self,
        plan: DiscoveryPlan | None = None,
        raw_jobs: list[RawJob] | list[dict] | None = None,
        profiles: dict[tuple[str, str], CompanyProfile] | None = None,
        now: datetime | None = None,
        stop_at: str | None = None,
        persist: bool = True,
        enrich_linkedin_signal: bool = False,
    ) -> PipelineReport:
        """Run the pipeline for ``plan``.

        ``raw_jobs`` replaces discovery with already-collected postings.
        ``stop_at`` may be 'before_contacts' (no contact discovery, no
        enrichment) or 'before_enrichment' (contact discovery only).
        """
        run_key = plan.run_key if plan is not None else (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        original_repo = self.repo
        original_access_repo = self.access.repo
        if not persist:
            self.repo = InertRepository()
            self.access.repo = self.repo
        try:
            with bind_run(run_key):
                return self._run(
                    run_key=run_key,
                    raw_jobs=raw_jobs,
                    plan=plan,
                    profiles=profiles,
                    now=now,
                    enrich_linkedin_signal=enrich_linkedin_signal,
                    stop_at=stop_at,
                )
        finally:
            self.repo = original_repo
            self.access.repo = original_access_repo

    # ------------------------------------------------------------------
    def _run(
        self,
        run_key,
        raw_jobs=None,
        plan=None,
        profiles=None,
        now=None,
        enrich_linkedin_signal=False,
        stop_at=None,
    ) -> PipelineReport:
        report = PipelineReport(run_key=run_key)
        if plan is not None:
            report.plan = plan.to_dict()
        now = now or datetime.now(timezone.utc)
        self._audit("PIPELINE_START")

        # --- 1. Discovery -------------------------------------------------
        with stage(self.log, "discovery", report.stages):
            if raw_jobs is not None:
                raw = [r if isinstance(r, RawJob) else RawJob.from_dict(r) for r in raw_jobs]
            elif plan is not None:
                registry = self._registry or build_registry(
                    self.settings, access=self.access, logger=self.log)
                discovered = registry.run(plan, repo=self.repo)
                raw = discovered.jobs
                report.source_status = discovered.source_status
                report.coverage = discovered.coverage.as_dict()
            else:
                raw = []

            if enrich_linkedin_signal and raw:
                try:
                    LinkedInApplicantEnricher(access=self.access, logger=self.log).enrich(raw)
                except Exception as exc:
                    report.append_pitfall(stage="LINKEDIN_SIGNAL", reason=str(exc))

            report.raw_jobs = len(raw)
            report.raw_job_records = [_json_safe(asdict(r)) for r in raw]
        if not raw:
            self._audit("PIPELINE_COMPLETE", result="no_raw_jobs")
            return report

        # --- 2. Normalization ---------------------------------------------
        with stage(self.log, "normalization", report.stages):
            normalized, screened = Normalizer(self.log).normalize(raw)
            report.normalized_jobs = len(normalized)
            report.normalized_job_records = [_json_safe(asdict(n)) for n in normalized]
            for job in screened:
                report.append_pitfall(stage="NORMALIZATION", reason=job.screen_reason,
                                      title=job.title, url=job.evidence_url)
                self._record_discarded(report, "NORMALIZATION", job.screen_reason, job)

        # --- 3. Job deduplication -----------------------------------------
        with stage(self.log, "job_deduplication", report.stages):
            jobs, duplicates = JobDeduplicator(self.log).dedupe(normalized)
            report.duplicate_jobs = len(duplicates)

        # --- 4. Freshness -------------------------------------------------
        with stage(self.log, "freshness", report.stages):
            fresh_jobs, stale = FreshnessFilter(self.settings, self.log).split(jobs, now=now)
            report.fresh_jobs = len(fresh_jobs)
            for job, result in stale:
                self._record_discarded(report, "FRESHNESS", result.reason, job,
                                       age_days=result.age_days)

        # --- 5. Company identification + deduplication ------------------
        with stage(self.log, "company_identification", report.stages):
            fresh: list[FreshCompany] = CompanyIdentifier(self.log).identify(fresh_jobs)
            report.companies = len(fresh)

        # --- 6. Qualification and official domains ------------------------
        with stage(self.log, "qualification", report.stages):
            built_profiles = self._build_profiles(fresh, profiles, now=now)
            gate = QualificationGate(self.settings, self.log)
            qual_results = gate.qualify(fresh, built_profiles)

            # Official domains come before size resolution; the result feeds
            # size resolution and contact discovery.
            self._resolve_domains(fresh, qual_results, report, now=now)

            # Free public-web size evidence for companies blocked only on
            # unknown headcount, then re-qualify them.
            qual_results = self._resolve_sizes(
                fresh, built_profiles, qual_results, report, now=now
            )
            result_map = {fc.company.dedup_key: r for fc, r in zip(fresh, qual_results)}

            report.qualification_records = [
                self._qualification_dict(fc, r) for fc, r in zip(fresh, qual_results)
            ]

        # --- 7. Persist every company --------------------------------------
        qualified: list[tuple[FreshCompany, QualifiedLead, str | None]] = []
        with stage(self.log, "persistence", report.stages):
            for fc in fresh:
                accepted = self._process_company(fc, result_map, built_profiles, report)
                if accepted is not None and accepted[1].status == QualificationStatus.QUALIFIED.value:
                    qualified.append(accepted)

        # --- 8. Free / public contact discovery: QUALIFIED only ------------
        if stop_at != "before_contacts":
            with stage(self.log, "contact_discovery", report.stages):
                self._discover_contacts(qualified, report)

            # --- 9. Enrichment waterfall: QUALIFIED only, after public discovery
            if stop_at != "before_enrichment":
                with stage(self.log, "enrichment", report.stages):
                    self._enrich(qualified, report)

        # --- Reporting ------------------------------------------------------
        report.access = {
            "camoufox_fallbacks": self.access.fallback_count,
            "camoufox_budget": self.settings.camoufox_budget_per_run,
        }

        self._audit(
            "PIPELINE_COMPLETE",
            raw_jobs=report.raw_jobs,
            companies=report.companies,
            qualified=len(report.qualified),
            needs_review=len(report.needs_review),
            rejected=len(report.rejected),
        )
        self.log.info(
            "pipeline_summary",
            raw_jobs=report.raw_jobs,
            fresh_jobs=report.fresh_jobs,
            companies=report.companies,
            qualified=len(report.qualified),
            needs_review=len(report.needs_review),
            rejected=len(report.rejected),
        )
        return report

    def _record_discarded(self, report, stage_name: str, reason: str, job, **details) -> None:
        """Count and persist a posting dropped before company identification."""
        key = f"{stage_name}:{reason.split(':')[0]}"
        report.discarded[key] = report.discarded.get(key, 0) + 1
        self.repo.insert_qualification_reason({
            "stage": stage_name, "reason": reason,
            "details": _json_safe({
                "company": job.company_name, "title": job.title, "url": job.evidence_url,
                "source_site": job.source_site, "posting_date": job.posting_date,
                "provenance": job.provenance or None, **details,
            }),
        })

    def _process_company(self, fc, result_map, built_profiles, report):
        """Persist one company and its fresh jobs, then record its outcome.

        Returns ``(fresh company, lead, company id)`` for a QUALIFIED or
        NEEDS_REVIEW company, else None.
        """
        aggregate = fc.company
        key = aggregate.dedup_key
        result = result_map.get(key)
        profile = built_profiles.get(key)

        company_id = self._persist_company(aggregate, result, profile)
        self._persist_jobs(fc, company_id)
        self._record_hiring_history(company_id, fc)

        if result is None:
            return

        # Every reason behind a REJECTED or NEEDS_REVIEW verdict is its own row.
        for reason in result.reasons + result.review_flags:
            self.repo.insert_qualification_reason({
                "company_id": company_id, "stage": "QUALIFICATION", "reason": reason,
                "outcome": ("REJECTED" if reason in result.reasons
                            else QualificationStatus.NEEDS_REVIEW.value),
            })

        if result.status == QualificationStatus.REJECTED.value:
            report.rejected.append(
                RejectedLead(
                    aggregate.company_name_normalized,
                    aggregate.domain,
                    "QUALIFICATION",
                    result.reasons,
                )
            )
            for reason in result.reasons:
                report.append_pitfall(
                    stage="QUALIFICATION",
                    company=aggregate.company_name_normalized,
                    reason=reason,
                )
            return

        lead = self._process_accepted(aggregate, fc, result, profile, company_id, report)
        if result.status == QualificationStatus.QUALIFIED.value:
            report.qualified.append(lead)
        else:
            report.needs_review.append(lead)

        return fc, lead, company_id

    # ------------------------------------------------------------------
    def _build_profiles(self, fresh, provided, now=None) -> dict[tuple[str, str], CompanyProfile]:
        """Assemble profiles, applying the size-evidence precedence.

        explicit provided -> cached DB size -> observed job-board size.
        The public-web resolver runs later, only for companies that reach
        NEEDS_REVIEW purely because size was unknown (see _resolve_sizes).
        """
        provided = provided or {}
        profiles: dict[tuple[str, str], CompanyProfile] = {}
        for fc in fresh:
            key = fc.company.dedup_key
            if key in provided:
                profiles[key] = provided[key]
                continue

            runs = 0
            cached = None
            if self.repo.configured:
                company = self.repo.find_company(key[0], key[1])
                if company:
                    runs = int(company.get("persistent_hiring_runs") or 0)
                cached_evidence = self._size_cache.lookup(key[0], key[1], now=now)
                if cached_evidence is not None:
                    cached = cached_evidence.as_enrichment()

            profiles[key] = build_profile(
                fc, persistent_hiring_runs=runs, enrichment=cached,
            )
        return profiles

    # ------------------------------------------------------------------
    def _resolve_sizes(self, fresh, profiles, results, report, now=None):
        """Second pass: resolve size for companies blocked ONLY on unknown size.

        Paid enrichment runs only on QUALIFIED companies, so a company flagged
        NEEDS_REVIEW for unknown size could never obtain the fact that would
        clear it. This closes that deadlock with free public-web evidence,
        then re-qualifies.

        Companies rejected for any other reason are never touched.
        """
        blocked = [
            (fc, res) for fc, res in zip(fresh, results)
            if res.status == QualificationStatus.NEEDS_REVIEW.value
            and "EMPLOYEE_SIZE_UNKNOWN" in res.review_flags
        ]
        stats = {
            "candidates": len(blocked), "resolved": 0, "unresolved": 0,
            "no_domain": 0, "in_range": 0, "oversize": 0, "undersize": 0,
            "newly_qualified": 0, "newly_rejected": 0,
            "domains_attempted": 0, "domains_resolved": 0,
            "domain_high": 0, "domain_medium": 0, "domain_rejected": 0,
        }
        if not blocked:
            report.size_resolution = stats
            return results

        self.log.info("size_resolution_start", candidates=len(blocked))
        gate = QualificationGate(self.settings, self.log)
        by_key = {fc.company.dedup_key: i for i, fc in enumerate(fresh)}
        updated = list(results)

        for fc, previous in blocked:
            company = fc.company
            # The domain stage has already run; reuse its verdict rather than
            # searching again.
            domain = self.domain_for(company)
            if not domain:
                stats["domains_attempted"] += 1
                stats["no_domain"] += 1
                stats["unresolved"] += 1
                continue
            if not company.domain:
                stats["domains_attempted"] += 1
                stats["domains_resolved"] += 1
                key_conf = self._domain_confidence.get(company.dedup_key)
                if key_conf == "HIGH":
                    stats["domain_high"] += 1
                elif key_conf == "MEDIUM":
                    stats["domain_medium"] += 1
            try:
                evidence = self._size_resolver.resolve(
                    domain, company.company_name_normalized
                )
            except Exception as exc:
                report.append_pitfall(
                    stage="SIZE_RESOLUTION",
                    company=company.company_name_normalized, reason=str(exc),
                )
                evidence = None

            if evidence is None:
                stats["unresolved"] += 1
                continue

            stats["resolved"] += 1
            key = company.dedup_key
            profile = build_profile(
                fc,
                persistent_hiring_runs=profiles[key].persistent_hiring_runs,
                enrichment=evidence.as_enrichment(),
            )
            profiles[key] = profile
            rescored = gate.qualify([fc], {key: profile})[0]
            updated[by_key[key]] = rescored

            verdict = rescored.breakdown.get("size_verdict")
            stats[{"IN_RANGE": "in_range", "OVERSIZE": "oversize",
                   "UNDERSIZE": "undersize"}.get(verdict, "unresolved")] += 1

            # Size is now known, so out-of-range is a decision, not a question.
            # The gate has already encoded it in `rescored`; this only counts.
            if verdict in ("OVERSIZE", "UNDERSIZE"):
                if rescored.status == QualificationStatus.REJECTED.value:
                    stats["newly_rejected"] += 1
            elif rescored.status == QualificationStatus.QUALIFIED.value:
                stats["newly_qualified"] += 1

            if evidence.url:
                self.repo.insert_evidence({
                    "record_type": "COMPANY", "key": "employee_size",
                    "value": f"{evidence.employee_size_min}-{evidence.employee_size_max}",
                    "url": evidence.url, "source_type": evidence.source,
                    "source_priority": int(SourcePriority.PUBLIC_WEB.value),
                })
            self.log.info(
                "size_resolution_applied",
                company=company.company_name_normalized,
                verdict=verdict, status=rescored.status, url=evidence.url,
            )

        report.size_resolution = stats
        self.log.info("size_resolution_complete", **stats)
        return updated

    # ------------------------------------------------------------------
    def _resolve_domains(self, fresh, results, report, now=None) -> dict:
        """Resolve an official domain for every fresh company that lacks one.

        Companies already carrying a trusted employer domain are skipped, as are
        companies the gate rejected outright - there is nothing to act on. The
        run-level budget stops a large plan from spending the whole run here.
        """
        stats = {"candidates": 0, "resolved": 0, "cached": 0, "unresolved": 0,
                 "skipped_budget": 0,
                 "budget": self.settings.domain_resolve_max_per_run}
        budget = self.settings.domain_resolve_max_per_run
        by_key = {fc.company.dedup_key: r for fc, r in zip(fresh, results)}

        def _worth(fc):
            """Order candidates by how much a domain would be worth.

            The budget is finite, so spend it on the companies most likely to
            become leads: a QUALIFIED company first, then one still in review,
            and within each the strongest hiring signal and freshest posting.
            """
            result = by_key.get(fc.company.dedup_key)
            status = getattr(result, "status", None)
            rank = {QualificationStatus.QUALIFIED.value: 0,
                    QualificationStatus.NEEDS_REVIEW.value: 1}.get(status, 2)
            age = fc.min_age_days
            return (rank, -fc.hiring_intensity, 99.0 if age is None else age)

        pending = [
            fc for fc in fresh
            if not fc.company.domain
            and getattr(by_key.get(fc.company.dedup_key), "status", None)
            != QualificationStatus.REJECTED.value
        ]
        pending.sort(key=_worth)
        stats["pending"] = len(pending)
        stats["time_budget_seconds"] = self.settings.domain_resolve_max_seconds
        started = _time.monotonic()

        for fc in pending:
            elapsed = _time.monotonic() - started
            if elapsed >= self.settings.domain_resolve_max_seconds:
                stats["skipped_time"] = len(pending) - stats["candidates"]
                stats["stopped_on"] = "TIME"
                self.log.info("domain_resolution_time_exhausted",
                              seconds=round(elapsed, 1),
                              skipped=stats["skipped_time"])
                break
            company = fc.company
            stats["candidates"] += 1
            cached_before = self._domain_cache.hits
            review_before = len(self._domain_review)
            searched_before = self._domain_searches
            found = self._resolve_domain(company, report, now=now)
            if found and self._domain_cache.hits > cached_before:
                stats["cached"] += 1
            elif found:
                stats["resolved"] += 1
            elif len(self._domain_review) > review_before:
                stats["needs_review"] = stats.get("needs_review", 0) + 1
            else:
                stats["unresolved"] += 1
            if self._domain_searches > searched_before:
                budget -= 1
                if budget <= 0:
                    stats["skipped_budget"] = len(pending) - stats["candidates"]
                    stats["stopped_on"] = "COUNT"
                    self.log.info("domain_resolution_budget_exhausted",
                                  limit=self.settings.domain_resolve_max_per_run,
                                  skipped=stats["skipped_budget"])
                    break

        stats["elapsed_seconds"] = round(_time.monotonic() - started, 1)
        report.domain_resolution = stats
        self.log.info("domain_resolution_complete", **stats)
        return stats

    def domain_for(self, company) -> str | None:
        """Strongest domain known for a company: source-provided, else resolved.

        A source-provided domain is never replaced by a searched one - it is the
        stronger evidence, and the deduplication key depends on it.
        """
        if getattr(company, "domain", None):
            return company.domain
        result = self._domain_results.get(company.dedup_key)
        return getattr(result, "domain", None)

    # ------------------------------------------------------------------
    def _resolve_domain(self, company, report, now=None) -> str | None:
        """Establish an official domain. Only HIGH/MEDIUM is attached.

        A verified domain persisted by an earlier run is reused outright; the
        search only runs when no valid cached domain exists.
        """
        key = company.dedup_key
        cached = self._domain_cache.lookup(key[0], key[1], now=now)
        if cached is not None:
            self._domain_confidence[key] = cached.confidence.value
            self._domain_results[key] = cached
            self.log.info("domain_cache_hit", company=company.company_name_normalized,
                          domain=cached.domain)
            return cached.domain

        # A recent failed attempt suppresses another expensive search until the
        # negative TTL expires. Never permanent.
        if self._domain_cache.suppresses_search(key[0], key[1], now=now):
            self.log.info("domain_negative_cache_hit",
                          company=company.company_name_normalized,
                          ttl_days=self.settings.domain_negative_cache_days)
            return None

        self._domain_searches += 1
        try:
            result = self._domain_resolver.resolve(
                company.company_name or company.company_name_normalized,
                existing_domain=company.domain or None,
                source_urls=[j.company_website for j in company.jobs if j.company_website],
                location=company.location,
            )
        except Exception as exc:
            report.append_pitfall(stage="DOMAIN_RESOLUTION",
                                  company=company.company_name_normalized,
                                  reason=str(exc))
            return None

        if not result.acceptable:
            # A plausible-but-unconfirmed candidate is kept for a human rather
            # than attached or thrown away.
            if getattr(result, "needs_review", False):
                self._domain_review[company.dedup_key] = {
                    "candidate_domain": result.domain,
                    "confidence": result.confidence.value,
                    "name_affinity": result.signals.get("name_affinity"),
                    "evidence_url": result.evidence_url,
                    "reason": result.reason,
                }
                self.log.info("domain_needs_review",
                              company=company.company_name_normalized,
                              candidate=result.domain,
                              confidence=result.confidence.value,
                              affinity=result.signals.get("name_affinity"))
                return None
            # Remember the failure so the next run does not repeat the search.
            self._domain_failures.add(company.dedup_key)
            self.log.info("domain_unresolved",
                          company=company.company_name_normalized,
                          confidence=result.confidence.value, reason=result.reason)
            return None

        self._domain_confidence[company.dedup_key] = result.confidence.value
        self._domain_results[company.dedup_key] = result
        return result.domain

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _persist_company(self, aggregate, result, profile) -> str | None:
        status = QualificationStatus.PENDING.value
        score = None
        reasons: list[str] = []
        flags: list[str] = []
        breakdown: dict = {}
        if result is not None:
            status = result.status
            score = result.score
            reasons = result.reasons
            flags = result.review_flags
            breakdown = result.breakdown

        normalized_domain, normalized_name = aggregate.dedup_key
        data = {
            "normalized_name": normalized_name,
            "normalized_domain": normalized_domain,
            "identity_basis": aggregate.identity_basis,
            "identity_key": aggregate.identity_key,
            "display_name": aggregate.company_name,
            "domain": aggregate.domain or None,
            "website": aggregate.company_website,
            "location": aggregate.location,
            "qualification_status": status,
            "qualification_score": score,
            "qualification_reasons": reasons,
            "qualification_breakdown": _json_safe(breakdown),
            "review_flags": flags,
            "hiring_intensity": aggregate.hiring_intensity,
            "min_applicant_count": aggregate.min_applicant_count,
            "source_type": aggregate.jobs[0].source_type if aggregate.jobs else None,
            "source_priority": aggregate.best_source_priority,
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "raw_payload": {"source_sites": aggregate.source_sites,
                            "source_ids": aggregate.source_ids},
        }

        if aggregate.dedup_key in self._domain_failures:
            data.update(negative_domain_row())

        resolved = self._domain_results.get(aggregate.dedup_key)
        if resolved is not None:
            # Only HIGH/MEDIUM reach here; domain_row() drops anything else.
            # normalized_domain stays the dedup key and is never rewritten.
            data.update(domain_row(resolved))

        if profile is not None:
            data.update(
                {
                    "industry": profile.industry,
                    "client_industry": profile.client_industry,
                    "industry_source": profile.industry_source,
                    "employee_size_min": profile.employee_size_min,
                    "employee_size_max": profile.employee_size_max,
                    "employee_size_source": profile.size_source,
                    "internal_ta_size": profile.internal_ta.ta_role_count,
                    "internal_ta_verdict": (
                        "MATURE" if result is not None
                        and result.breakdown.get("internal_ta_verdict") == "MATURE" else "OK"),
                }
            )
        if result is not None:
            data["is_staffing_agency"] = bool(result.breakdown.get("is_staffing_agency"))
            data["is_direct_employer"] = not data["is_staffing_agency"]

        return self.repo.upsert_company(data)

    def _persist_jobs(self, fresh, company_id) -> None:
        for job, res in fresh.fresh_jobs:
            job_id = self.repo.upsert_job(
                {
                    "company_id": company_id,
                    "external_id": job.external_id or job.application_url,
                    "title": job.title,
                    "title_normalized": job.title_normalized,
                    "location": job.location,
                    "location_normalized": job.location_normalized,
                    "state": job.state,
                    "country": job.country,
                    "description": (job.description or "")[:20000] or None,
                    "posting_date": job.posting_date,
                    "posted_at": job.posted_at.isoformat() if job.posted_at else None,
                    "date_precision": job.date_precision,
                    "age_days": round(res.age_days, 3) if res.age_days is not None else None,
                    "application_url": job.application_url,
                    "evidence_url": job.evidence_url,
                    "ats_platform": job.ats_platform,
                    "is_fresh": res.keep,
                    "freshness_reason": res.reason,
                    "applicant_count": job.applicant_count,
                    "source_type": job.source_type,
                    "source_site": job.source_site,
                    "source_priority": job.source_priority,
                    "last_seen_at": datetime.now(timezone.utc).isoformat(),
                    "raw_payload": _json_safe(job.raw),
                    "provenance": _json_safe(job.provenance) or None,
                }
            )
            if job.posting_date:
                self.repo.insert_evidence(
                    {
                        "record_type": "JOB",
                        "record_id": job_id,
                        "key": "posting_date",
                        "value": job.posting_date,
                        "url": job.evidence_url,
                        "source_type": job.source_type,
                        "source_priority": job.source_priority,
                    }
                )

    def _record_hiring_history(self, company_id, fresh) -> None:
        if company_id is None:
            return
        self.repo.upsert_hiring_history(
            {
                "company_id": company_id,
                "observed_on": date.today().isoformat(),
                "open_jobs": fresh.hiring_intensity,
                "fresh_jobs": fresh.hiring_intensity,
            }
        )
        runs = self.repo.count_hiring_history(company_id)
        if runs:
            self.repo.update_company(company_id, {"persistent_hiring_runs": runs})

    # ------------------------------------------------------------------
    # The lead record for a QUALIFIED or NEEDS_REVIEW company
    # ------------------------------------------------------------------
    def _process_accepted(
        self, aggregate, fresh, result, profile, company_id, report
    ) -> QualifiedLead:
        company_name = aggregate.company_name_normalized
        # Prefer a verified official domain over the board's profile URL; that
        # is the entire point of resolving one.
        resolved_domain = self.domain_for(aggregate)
        website = aggregate.company_website
        if not website and resolved_domain:
            website = f"https://{resolved_domain}"
        job_urls = [j.evidence_url for j, _ in fresh.fresh_jobs if j.evidence_url]
        return QualifiedLead(
            company_name=company_name,
            rejection_reasons=list(result.reasons),
            display_name=aggregate.company_name,
            domain=aggregate.domain or resolved_domain,
            website=website,
            location=aggregate.location,
            status=result.status,
            score=result.score,
            job_count=fresh.hiring_intensity,
            freshest_job_age_days=fresh.min_age_days,
            client_industry=getattr(profile, "client_industry", None),
            industry_source=getattr(profile, "industry_source", None),
            employee_size=_size_label(profile),
            employee_size_source=getattr(profile, "size_source", None),
            applicant_count=aggregate.min_applicant_count,
            review_flags=result.review_flags,
            evidence_url=job_urls[0] if job_urls else None,
            source_sites=aggregate.source_sites,
        )

    # ------------------------------------------------------------------
    # Free / public contact discovery (QUALIFIED companies only)
    # ------------------------------------------------------------------
    def _discover_contacts(self, qualified, report) -> None:
        """Find public contacts for QUALIFIED companies, strongest hiring first.

        NEEDS_REVIEW and REJECTED companies never reach this method. At most
        ``CONTACTS_MAX_COMPANIES_PER_RUN`` companies are searched; the rest are
        marked SKIPPED_COMPANY_BUDGET.
        """
        stats = {"qualified": len(qualified), "searched": 0, "skipped_budget": 0,
                 "page_budget_exhausted": 0, "failed": 0, "pages_fetched": 0,
                 "contacts": 0, "emails": 0,
                 "company_budget": self.settings.contacts_max_companies_per_run}
        for index, (fc, lead, company_id) in enumerate(sorted(qualified, key=_hiring_strength)):
            if index >= self.settings.contacts_max_companies_per_run:
                lead.contact_discovery = "SKIPPED_COMPANY_BUDGET"
                stats["skipped_budget"] += 1
                continue
            stats["searched"] += 1
            self._contacts_for(fc, lead, company_id, report, stats)
        report.contact_discovery = stats
        self.log.info("contact_discovery_complete", **stats)

    # ------------------------------------------------------------------
    # Enrichment waterfall (QUALIFIED companies only, after public discovery)
    # ------------------------------------------------------------------
    def _enrich(self, qualified, report) -> None:
        """ZoomInfo first; Apollo and Apify only as fallbacks. Strongest hiring first.

        See ``nexbase/enrichment/waterfall.py`` for when a fallback is called.
        Unconfigured providers are recorded as PROVIDER_UNAVAILABLE and never billed.
        """
        providers = self._enrichment_providers or [
            ZoomInfoAdapter(self.settings, logger=self.log),
            ApolloAdapter(self.settings, logger=self.log),
            ApifyAdapter(self.settings),
        ]
        waterfall = EnrichmentWaterfall(providers, self.repo, self.settings, self.log)
        statuses: dict[str, dict[str, int]] = {p.name: {} for p in providers}
        credits: dict[str, float] = {p.name: 0.0 for p in providers}
        for fc, lead, company_id in sorted(qualified, key=_hiring_strength):
            company = CompanyContext(
                name=lead.display_name or lead.company_name,
                name_normalized=fc.company.company_name_normalized,
                domain=lead.domain or None, states=frozenset(fc.company.states))
            try:
                result = waterfall.enrich_company(company, lead, company_id)
            except Exception as exc:
                report.append_pitfall(stage="ENRICHMENT", company=lead.company_name,
                                      reason=str(exc))
                continue
            lead.enrichment = result.as_dict()
            for attempt in result.attempts:
                counts = statuses[attempt["provider"]]
                counts[attempt["status"]] = counts.get(attempt["status"], 0) + 1
                credits[attempt["provider"]] += attempt.get("credits") or 0.0
            if result.provider_emails or any(
                    a.get("contacts_added") or a.get("contacts_upgraded") for a in result.attempts):
                self._reclassify_emails(lead, company_id, result.provider_emails)
        report.enrichment = {"qualified": len(qualified), "statuses": statuses,
                             "credits": credits}
        self.log.info("enrichment_complete", statuses=statuses, credits=credits)

    def _reclassify_emails(self, lead, company_id, provider_emails) -> None:
        """Classify again with the enriched contacts; persist only new addresses."""
        seen = {o["email"] for o in lead.observed_emails}
        emails = EmailDiscovery(self.log).discover(
            lead.contacts, extra_emails=list(lead.observed_emails) + list(provider_emails),
            company_domain=lead.domain or None)
        self._persist_emails(company_id, [o for o in emails.observed if o.email not in seen])
        lead.emails = emails.preferred
        lead.email_status = emails.status
        lead.observed_emails = [o.as_dict() for o in emails.observed]
        lead.named_contact_emails = [o.as_dict() for o in emails.named]
        lead.role_mailboxes = list(emails.role)

    def _contacts_for(self, fc, lead, company_id, report, stats) -> None:
        jobs = [job for job, _ in fc.fresh_jobs]
        try:
            found = ContactDiscovery(
                access=self.access, settings=self.settings, logger=self.log
            ).discover(
                lead.display_name or lead.company_name,
                lead.website,
                [j.evidence_url for j in jobs if j.evidence_url],
                list(dict.fromkeys(j.company_url for j in jobs if j.company_url)),
            )
        except Exception as exc:
            report.append_pitfall(stage="CONTACT_DISCOVERY", company=lead.company_name,
                                  reason=str(exc))
            lead.contact_discovery = "FAILED"
            stats["failed"] += 1
            return

        selected = ContactRanker(self.settings, self.log).select(
            found.candidates, [j.title for j in jobs])
        lead.contacts = self._persist_contacts(company_id, selected)

        # 1. Addresses already on the postings, then 2. those on pages visited.
        posting_emails = [
            {"email": email, "source": job.source_site, "source_type": job.source_type,
             "evidence_url": job.evidence_url, "extraction_method": "job_posting",
             "discovery_stage": DiscoveryStage.SAME_SOURCE.value}
            for job in jobs for email in job.observed_emails or []
        ]
        emails = EmailDiscovery(self.log).discover(
            lead.contacts, extra_emails=posting_emails + found.page_emails,
            company_domain=lead.domain or None)
        self._persist_emails(company_id, emails.observed)

        lead.emails = emails.preferred
        lead.email_status = emails.status
        lead.observed_emails = [o.as_dict() for o in emails.observed]
        lead.named_contact_emails = [o.as_dict() for o in emails.named]
        lead.role_mailboxes = list(emails.role)
        lead.discovery_stages = list(found.stages_used)
        lead.portals_searched = sorted({j.source_site for j in fc.company.jobs if j.source_site})
        resolved = self._domain_results.get(fc.company.dedup_key)
        if resolved is not None:
            lead.domain_confidence = getattr(getattr(resolved, "confidence", None), "value", None)
            lead.domain_source = getattr(resolved, "source", None)
        lead.contact_discovery = (
            "PAGE_BUDGET_EXHAUSTED" if found.budget_exhausted else "COMPLETED")

        stats["page_budget_exhausted"] += found.budget_exhausted
        stats["pages_fetched"] += found.pages_fetched
        stats["contacts"] += len(lead.contacts)
        stats["emails"] += len(emails.observed)

    def _persist_contacts(self, company_id, candidates) -> list[dict]:
        """Store each ranked contact with its provenance and an evidence row."""
        out: list[dict] = []
        for c in candidates:
            contact_id = self.repo.upsert_contact({
                "company_id": company_id,
                "name": c.name,
                "title": c.title,
                "title_priority": c.title_priority,
                "email": c.email,
                "rank_score": c.rank_score,
                "discovery_stage": c.discovery_stage,
                "profile_url": c.profile_url,
                "source_type": c.source_type,
                "source_priority": c.source_priority,
                "raw_payload": _json_safe(c.raw),
            })
            self.repo.insert_evidence({
                "record_type": "CONTACT",
                "record_id": contact_id,
                "key": "contact",
                "value": f"{c.name} | {c.title}",
                "url": c.url,
                "source_type": c.source_type,
                "source_priority": c.source_priority,
                "raw_payload": _json_safe({
                    "discovery_stage": c.discovery_stage,
                    "title_priority": c.title_priority,
                    "rank_score": c.rank_score,
                    "confidence": c.confidence,
                    **c.raw,
                }),
            })
            out.append({
                "id": contact_id,
                "name": c.name,
                "title": c.title,
                "title_priority": c.title_priority,
                "priority": c.priority,
                "email": c.email,
                "rank_score": c.rank_score,
                "discovery_stage": c.discovery_stage,
                "source_type": c.source_type,
                "source": (urlsplit(c.url or "").hostname or "").lower() or None,
                "source_url": c.url,
                "profile_url": c.profile_url,
                "extraction": c.raw.get("extraction"),
                "origin": "PUBLIC",
            })
        return out

    def _persist_emails(self, company_id, observed) -> None:
        """Store every observed address as company evidence with its provenance.

        ``email_type`` is the email class (PERSONAL, ROLE, PORTAL_GENERATED,
        EXTERNAL_UNVERIFIED, DOMAIN_MISMATCH); low-confidence classes are kept as
        evidence. The API reads these rows back as the company's emails.
        """
        for o in observed:
            self.repo.insert_evidence({
                "record_type": "COMPANY",
                "record_id": company_id,
                "key": "company_email",
                "value": o.email,
                "url": o.evidence_url,
                "source_type": o.source_type,
                "source_priority": int(
                    SourcePriority.PAID_ENRICHMENT.value if o.source_type == "ZOOMINFO"
                    else SourcePriority.PUBLIC_WEB.value),
                "raw_payload": _json_safe({"email_type": o.email_class, **o.as_dict()}),
            })

    # ------------------------------------------------------------------
    @staticmethod
    def _qualification_dict(fc: FreshCompany, result: QualificationResult) -> dict:
        return {
            "company": fc.company.company_name_normalized,
            "display_name": fc.company.company_name,
            "domain": fc.company.domain,
            "status": result.status,
            "score": result.score,
            "reasons": result.reasons,
            "review_flags": result.review_flags,
            "breakdown": _json_safe(result.breakdown),
            "source_sites": fc.company.source_sites,
        }


def _hiring_strength(item) -> tuple:
    """Strongest hiring first: more fresh openings, then the freshest posting."""
    fc = item[0]
    age = fc.min_age_days
    return (-fc.hiring_intensity, 99.0 if age is None else age)


def _size_label(profile) -> str | None:
    if profile is None or not getattr(profile, "size_known", False):
        return None
    lo, hi = profile.employee_size_min, profile.employee_size_max
    if lo is not None and hi is not None:
        return f"{lo}-{hi}" if lo != hi else str(lo)
    if lo is not None:
        return f"{lo}+"
    if hi is not None:
        return f"<{hi}"
    return None
