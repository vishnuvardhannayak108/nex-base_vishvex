"""FastAPI application exposing the NexBase pipeline.

Endpoints:
  GET  /health                    liveness + schema check (public)
  POST /pipeline/run              trigger a pipeline run
  GET  /leads                     paginated leads (the export point)
  GET  /companies/{id}/emails     contacts and mailboxes with provenance
  GET  /config/sectors            sector list and suggested job titles

Every endpoint except ``/health`` requires ``X-API-Key``; outside a development
environment an unset ``NEXBASE_API_KEY`` fails closed.
"""
from __future__ import annotations

import hmac
import os
import time
from collections import deque
from threading import Lock
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator

from nexbase.config import get_settings
from nexbase.core.models import RawJob
from nexbase.db.repository import SupabaseRepository
from nexbase.core.errors import DiscoveryError
from nexbase.discovery.planner import NATIONWIDE, build_config, known_sectors
from nexbase.logging_setup import ensure_logging_configured, get_logger

settings = get_settings()
ensure_logging_configured(settings.log_level)
log = get_logger("nexbase.api")

API_KEY = os.getenv("NEXBASE_API_KEY", "")
#: Only a development environment may run without a key. Anywhere else an unset
#: key is a misconfiguration, and answering the request anyway would expose the
#: pipeline trigger and the lead database to anyone who found the port.
DEV_ENVIRONMENTS = {"development", "dev", "test", "local"}

app = FastAPI(title="NexBase API", version="0.3.0")


#: Inbound rate limit. Deliberately in-process and dependency-free: it exists
#: to stop one caller hammering `/pipeline/run`, which starts large scrapes,
#: not to be a distributed quota.
RATE_LIMIT_REQUESTS = int(os.getenv("NEXBASE_API_RATE_LIMIT", "60"))
RATE_LIMIT_WINDOW_SECONDS = 60.0
_rate_hits: dict[str, deque] = {}
_rate_lock = Lock()


def rate_limit(request: Request) -> None:
    """Reject a caller that exceeds the per-minute request budget."""
    if RATE_LIMIT_REQUESTS <= 0:
        return
    caller = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with _rate_lock:
        hits = _rate_hits.setdefault(caller, deque())
        while hits and now - hits[0] > RATE_LIMIT_WINDOW_SECONDS:
            hits.popleft()
        if len(hits) >= RATE_LIMIT_REQUESTS:
            raise HTTPException(
                status_code=429,
                detail=f"rate limit exceeded ({RATE_LIMIT_REQUESTS}/min)",
                headers={"Retry-After": str(int(RATE_LIMIT_WINDOW_SECONDS))},
            )
        hits.append(now)


def _auth_open_for_development() -> bool:
    return settings.environment.strip().lower() in DEV_ENVIRONMENTS


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Authenticate a request, failing closed when no key is configured."""
    if not API_KEY:
        if _auth_open_for_development():
            return
        raise HTTPException(
            status_code=503,
            detail=(
                "NEXBASE_API_KEY is not set; refusing to serve authenticated "
                "endpoints outside a development environment"
            ),
        )
    if not x_api_key or not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


def get_repo() -> SupabaseRepository:
    return SupabaseRepository()


# ---------------------------------------------------------------------------
#: Upper bound on the free-form dictionaries a caller may submit. They are
#: splatted into discovery calls, so unbounded input is unbounded work.
MAX_RAW_JOBS = 5000
MAX_PARAM_KEYS = 40


def _bounded_params(value: dict[str, Any] | None, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if len(value) > MAX_PARAM_KEYS:
        raise ValueError(f"{label} has too many keys (max {MAX_PARAM_KEYS})")
    return value


class SizeRange(BaseModel):
    """Operator-selected employee band for one run.

    This is a *filter*, never evidence: it decides what we are looking for and
    must not overwrite an employee count a source actually published.
    """

    minimum: int = Field(default=11, ge=1, le=100000)
    maximum: int = Field(default=200, ge=1, le=100000)

    @field_validator("maximum")
    @classmethod
    def _ordered(cls, value, info):
        minimum = info.data.get("minimum")
        if minimum is not None and value < minimum:
            raise ValueError("maximum must be greater than or equal to minimum")
        return value


class DiscoveryConfigRequest(BaseModel):
    """The operator's choices for one run. Authoritative; nothing is invented.

    ``location`` defaults to "USA", which means nationwide United States and is
    sent to each source as a single scope - never expanded into states or
    metros. ``search_terms`` is required: NexBase does not generate terms, and a
    run with none would otherwise silently become a run with ours.
    """

    search_terms: list[str] = Field(min_length=1, max_length=200)
    sector: str | None = None
    location: str = NATIONWIDE
    size_min: int | None = Field(default=None, ge=1, le=100000)
    size_max: int | None = Field(default=None, ge=1, le=100000)
    freshness_days: int | None = Field(default=None, ge=1, le=365)
    jobspy_sites: list[str] | None = Field(default=None, max_length=20)
    board_sites: list[str] | None = Field(default=None, max_length=20)
    ats_slices: list[str] | None = Field(default=None, max_length=50)
    max_pages_per_query: int | None = Field(default=None, ge=1, le=500)
    max_results_per_query: int | None = Field(default=None, ge=1, le=100000)

    @field_validator("sector")
    @classmethod
    def _known_sector(cls, value):
        if value and value not in known_sectors():
            raise ValueError(f"sector must be one of {known_sectors()}")
        return value

    def to_config(self):
        """Validated by the planner too - this is the only way in."""
        return build_config(
            search_terms=self.search_terms,
            sector=self.sector,
            location=self.location,
            size_min=self.size_min,
            size_max=self.size_max,
            freshness_days=self.freshness_days,
            jobspy_sites=self.jobspy_sites,
            board_sites=self.board_sites,
            ats_slices=self.ats_slices,
            max_pages_per_query=self.max_pages_per_query,
            max_results_per_query=self.max_results_per_query,
        )


class RunRequest(BaseModel):
    raw_jobs: list[dict[str, Any]] | None = Field(default=None, max_length=MAX_RAW_JOBS)
    ats_params: dict[str, Any] | None = None
    jobspy_params: dict[str, Any] | None = None
    board_params: dict[str, Any] | None = None
    #: The operator's discovery configuration. Autonomous planning is not
    #: part of the product: without this (and without raw_jobs or explicit
    #: source params) there is nothing to discover.
    discovery: DiscoveryConfigRequest | None = None
    stop_at: str | None = None
    persist: bool = True
    enrich_linkedin_signal: bool = False
    include_records: bool = False
    #: Overrides the configured band for this run only.
    size_range: SizeRange | None = None

    @field_validator("ats_params", "jobspy_params", "board_params")
    @classmethod
    def _check_params(cls, value, info):
        return _bounded_params(value, info.field_name)

    @field_validator("stop_at")
    @classmethod
    def _check_stop_at(cls, value):
        allowed = {None, "before_contacts"}
        if value not in allowed:
            raise ValueError(f"stop_at must be one of {sorted(a for a in allowed if a)}")
        return value


# ---------------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    repo = SupabaseRepository()
    return {"status": "ok", "version": "0.3.0", "database": repo.health_check()}


@app.post("/pipeline/run", dependencies=[Depends(rate_limit), Depends(require_api_key)])
async def run_pipeline(request: RunRequest) -> dict:
    from nexbase.discovery.planner import DiscoveryPlanner
    from nexbase.pipeline.runner import PipelineRunner

    raw = [RawJob.from_dict(d) for d in request.raw_jobs] if request.raw_jobs else None

    run_settings = settings
    plan = None
    if request.discovery is not None:
        try:
            config = request.discovery.to_config()
        except DiscoveryError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        plan = DiscoveryPlanner(settings).plan(config)
        # The band the operator chose for THIS run. The plan resolved it
        # against the configured defaults already.
        run_settings = settings.model_copy(update={
            "size_filter_min": plan.size_min,
            "size_filter_max": plan.size_max,
        })
        log.info("discovery_config_accepted", sector=config.sector,
                 location=config.location, nationwide=config.nationwide,
                 terms=len(config.search_terms))

    if request.size_range is not None:
        # model_copy keeps every other setting intact and leaves the process
        # default untouched, so one run's band cannot leak into the next.
        run_settings = run_settings.model_copy(update={
            "size_filter_min": request.size_range.minimum,
            "size_filter_max": request.size_range.maximum,
        })
        log.info("size_range_override", minimum=request.size_range.minimum,
                 maximum=request.size_range.maximum)

    runner = PipelineRunner(run_settings)
    report = await run_in_threadpool(
        runner.run,
        raw_jobs=raw,
        ats_params=request.ats_params,
        jobspy_params=request.jobspy_params,
        board_params=request.board_params,
        plan=plan,
        persist=request.persist,
        stop_at=request.stop_at,
        enrich_linkedin_signal=request.enrich_linkedin_signal,
    )
    log.info(
        "pipeline_run_complete",
        qualified=len(report.qualified),
        needs_review=len(report.needs_review),
        rejected=len(report.rejected),
    )
    # Full job records are opt-in; the default response stays bounded.
    return report.to_dict(include_records=request.include_records)


@app.get("/leads", dependencies=[Depends(rate_limit), Depends(require_api_key)])
def leads(
    status: str = Query(default="QUALIFIED"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    repo: SupabaseRepository = Depends(get_repo),
) -> dict:
    if not repo.configured:
        return {"configured": False, "items": [], "count": 0}
    filters = None if status == "ALL" else {"qualification_status": status}
    rows = repo.select(
        "companies", "*", filters=filters, limit=limit + offset, order_by="qualification_score"
    )
    page = rows[offset : offset + limit]
    return {"configured": True, "count": len(page), "offset": offset, "items": page}


@app.get("/config/sectors", dependencies=[Depends(require_api_key)])
def sectors(sector: str | None = Query(default=None)) -> dict:
    """Sector list, and suggested search terms for one of them.

    Suggestions only. What gets searched is whatever the operator sends in
    ``discovery.search_terms``.
    """
    from nexbase.discovery import taxonomy

    payload: dict = {"sectors": known_sectors(), "default_location": NATIONWIDE}
    if sector:
        payload["sector"] = sector
        payload["suggested_search_terms"] = taxonomy.suggest_terms_for_sector(
            sector, count=50)
    return payload


@app.get("/companies/{company_id}/emails", dependencies=[Depends(rate_limit), Depends(require_api_key)])
def company_emails(
    company_id: str,
    repo: SupabaseRepository = Depends(get_repo),
) -> dict:
    """Contacts and company mailboxes discovered for one company, with provenance.

    Page-only addresses are stored as company evidence rather than as fake
    people, so they are read back from `evidence` and returned alongside the
    named contacts.
    """
    if not repo.configured:
        return {"configured": False, "contacts": [], "company_emails": []}

    contacts = repo.select(
        "contacts",
        "id, name, title, title_priority, email, verification_status, "
        "discovery_stage, source_type",
        filters={"company_id": company_id},
        limit=100,
    )
    evidence = [
        row for row in repo.select(
            "evidence",
            "value, url, source_type, source_priority, raw_payload, created_at",
            filters={"record_type": "COMPANY", "record_id": company_id},
            limit=200,
        )
        if (row.get("raw_payload") or {}).get("email_type")
    ]
    return {
        "configured": True,
        "company_id": company_id,
        "contacts": contacts,
        "company_emails": evidence,
    }
