"""Benchmark for comparing candidate Apify enrichment actors before selecting one.

Nothing here selects or registers an actor. Actor definitions are supplied by the
caller (``scripts/benchmark_apify_actors.py --actors``), run through the same
``ApifyAdapter`` invoke / identity / attach steps the waterfall uses, and scored:

* POC coverage            companies with >= 1 attached P1-P4 contact
* correct-company rate    attached (identity confirmed) / parsed items
* P1-P4 title match rate  attached contacts with a POC title / attached
* email classes           PERSONAL, ROLE, EXTERNAL_UNVERIFIED, PORTAL_GENERATED,
                          DOMAIN_MISMATCH and NO_EMAIL, per NexBase classification
* duplicate rate          repeated people (profile URL, email or name) / attached
* runtime                 total, mean and max seconds per company
* provider errors         failed runs by PROVIDER_* status
* cost                    the per-run USD cap always; actual Apify usage only
                          when the run function reports it (``usage_total_usd``)
* identity statuses and risks (LinkedIn scraping needs client approval)

The run-sync endpoint returns dataset items only, so actual usage is ``None``
unless a run function that reads it is supplied. Live runs need client
credentials; the benchmark has not been run.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from nexbase.config import Settings
from nexbase.contacts.extraction import infer_priority
from nexbase.discovery.apify_sources import run_actor
from nexbase.email.discovery import classify_email
from nexbase.enrichment.apify import (
    COMPANY_TO_POCS,
    PROFILE_TO_EMAIL,
    ApifyAdapter,
    ApifyEnrichmentActor,
    linkedin_company_identity,
)
from nexbase.enrichment.base import FAILURE_STATUSES, CompanyContext, EnrichmentNeed, utcnow
from nexbase.enrichment.waterfall import person_key
from nexbase.pipeline.normalize import normalize_company_name


@dataclass
class BenchmarkCompany:
    name: str
    domain: str
    state: str | None = None
    #: For profile_to_email: people with ``name``, ``title`` and ``profile_url``.
    known_people: list[dict] = field(default_factory=list)

    def context(self) -> CompanyContext:
        return CompanyContext(name=self.name, name_normalized=normalize_company_name(self.name),
                              domain=self.domain,
                              states=frozenset({self.state}) if self.state else frozenset())


def load_companies(path: str | Path) -> list[BenchmarkCompany]:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    return [BenchmarkCompany(name=r["name"], domain=r["domain"], state=r.get("state"),
                             known_people=list(r.get("known_people") or [])) for r in rows]


def _identity_key(contact) -> str:
    return ((contact.profile_url or "").lower().rstrip("/")
            or (contact.email or "").lower()
            or "|".join(person_key(contact.name) or ("", "")))


def benchmark_actor(actor: ApifyEnrichmentActor, companies: list[BenchmarkCompany],
                    settings: Settings, *, max_items: int = 25,
                    run: Callable[..., list[dict] | tuple[list[dict], dict]] = run_actor,
                    clock: Callable[[], float] = time.monotonic) -> dict:
    """Run ``actor`` once per company and score it. Makes real calls with ``run_actor``."""
    usage: dict = {}

    def run_capturing_usage(*args, **kwargs):
        result = run(*args, **kwargs)
        if isinstance(result, tuple):
            items, usage["last"] = result
            return items
        usage["last"] = None
        return result

    adapter = ApifyAdapter(settings, actors={actor.actor_id: actor}, run=run_capturing_usage)
    rows = []
    for company in companies:
        context = company.context()
        need = EnrichmentNeed(full=actor.job == COMPANY_TO_POCS,
                              people=company.known_people, limit=max_items)
        started = clock()
        items, call = adapter.invoke(actor, context, need)
        runtime = clock() - started
        row = {"company": company.name, "domain": company.domain, "status": call.status,
               "error": call.error, "runtime_seconds": round(runtime, 3),
               "items_returned": call.records, "items_parsed": len(items),
               "cost_cap_usd": call.credit_cost if call.status not in FAILURE_STATUSES else 0.0,
               "usage_total_usd": (usage.get("last") or {}).get("usage_total_usd"),
               "identity_status": None, "attached": []}
        if call.status not in FAILURE_STATUSES:
            if actor.job == COMPANY_TO_POCS:
                row["identity_status"] = linkedin_company_identity(company.domain, items).status
                attached, _ = adapter.attach_company_pocs(context, items, actor)
            else:
                attached, _ = adapter.attach_profile_emails(context, need, items, actor)
            row["attached"] = attached
        rows.append(row)
    return _score(actor, rows)


def _score(actor: ApifyEnrichmentActor, rows: list[dict]) -> dict:
    parsed = sum(r["items_parsed"] for r in rows)
    attached = [(r, c) for r in rows for c in r["attached"]]
    poc = [(r, c) for r, c in attached if infer_priority(c.title) is not None]
    classes = Counter(classify_email(c.email, r["domain"]) if c.email else "NO_EMAIL"
                      for r, c in attached)
    duplicates = 0
    for row in rows:
        keys = [_identity_key(c) for c in row["attached"]]
        duplicates += len(keys) - len(set(keys))
    runtimes = [r["runtime_seconds"] for r in rows]
    actual = [r["usage_total_usd"] for r in rows if r["usage_total_usd"] is not None]
    companies_with_poc = {r["company"] for r, _ in poc}

    def rate(numerator, denominator):
        return round(numerator / denominator, 3) if denominator else None

    return {
        "actor_id": actor.actor_id, "job": actor.job, "risks": actor.risks,
        "companies": len(rows),
        "poc_coverage": rate(len(companies_with_poc), len(rows)),
        "items_returned": sum(r["items_returned"] for r in rows),
        "items_parsed": parsed,
        "contacts_attached": len(attached),
        "correct_company_rate": rate(len(attached), parsed),
        "title_match_rate": rate(len(poc), len(attached)),
        "email_classes": dict(classes),
        "duplicate_rate": rate(duplicates, len(attached)),
        "runtime_seconds": {"total": round(sum(runtimes), 3),
                            "mean": rate(sum(runtimes), len(runtimes)),
                            "max": max(runtimes) if runtimes else None},
        "errors": dict(Counter(r["status"] for r in rows if r["status"] in FAILURE_STATUSES)),
        "identity_statuses": dict(Counter(r["identity_status"] for r in rows
                                          if r["identity_status"] is not None)),
        "cost": {"cap_usd_total": round(sum(r["cost_cap_usd"] for r in rows), 4),
                 "actual_usd_total": round(sum(actual), 4) if actual else None,
                 "actual_reported_runs": len(actual)},
        "runs": [{k: v for k, v in r.items() if k != "attached"}
                 | {"attached": [{"name": c.name, "title": c.title, "email": c.email,
                                  "profile_url": c.profile_url} for c in r["attached"]]}
                 for r in rows],
    }


def run_benchmark(actors: list[ApifyEnrichmentActor], companies: list[BenchmarkCompany],
                  settings: Settings, **kwargs) -> dict:
    return {
        "generated_at": utcnow(),
        "live_validation": "Not a validation result: benchmarks compare candidate actors.",
        "jobs": sorted({a.job for a in actors}),
        "results": [benchmark_actor(a, [c for c in companies if a.job == COMPANY_TO_POCS
                                        or (a.job == PROFILE_TO_EMAIL and c.known_people)],
                                    settings, **kwargs)
                    for a in actors],
    }
