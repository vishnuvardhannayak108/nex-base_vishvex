"""Live source smoke test. Discovery only - never enriches, never queues.

Reports what each source actually did, including the ones that fail. A source
that returns nothing is reported as empty, not quietly omitted, and no
workaround is applied on its behalf.

Every enabled source in the registry runs one query through its own adapter.

    python scripts/live_smoke.py
    python scripts/live_smoke.py --term "warehouse associate" --state OH
    python scripts/live_smoke.py --json report.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nexbase.access.fetcher import AccessLayer  # noqa: E402
from nexbase.config import get_settings  # noqa: E402
from nexbase.core.models import RawJob  # noqa: E402
from nexbase.pipeline.dedupe import Deduplicator  # noqa: E402
from nexbase.pipeline.freshness import FreshnessFilter  # noqa: E402
from nexbase.pipeline.normalize import Normalizer  # noqa: E402


def _outcome(jobs: list[RawJob], error: str | None) -> str:
    if error:
        return "ERROR"
    return "SUCCESS" if jobs else "EMPTY"


def _source_row(name: str, jobs: list[RawJob], error: str | None,
                seconds: float, settings, now) -> dict:
    fresh_filter = FreshnessFilter(settings)
    normalized, _ = Normalizer().normalize(jobs)
    dated = [j for j in jobs if j.posted_at]
    fresh = [
        j for j in normalized
        if fresh_filter.evaluate(j, now=now).keep
    ]
    return {
        "source": name,
        "outcome": _outcome(jobs, error),
        "error": error,
        "jobs_returned": len(jobs),
        "dated_jobs": len(dated),
        "fresh_jobs": len(fresh),
        "with_company_name": sum(1 for j in jobs if j.company_name),
        "with_domain": sum(1 for j in normalized if j.domain),
        "with_description": sum(1 for j in jobs if j.description),
        "with_employee_count": sum(1 for j in jobs if j.company_employee_count),
        "with_industry": sum(1 for j in jobs if j.company_industry),
        "extraction_completeness": {
            "title": round(sum(1 for j in jobs if j.title) / len(jobs), 3) if jobs else 0,
            "company": round(
                sum(1 for j in jobs if j.company_name) / len(jobs), 3) if jobs else 0,
            "date": round(len(dated) / len(jobs), 3) if jobs else 0,
            "description": round(
                sum(1 for j in jobs if j.description) / len(jobs), 3) if jobs else 0,
        },
        "raw_observed_emails": sorted(
            {e for j in jobs for e in (j.observed_emails or [])}
        ),
        "runtime_seconds": round(seconds, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--term", default="warehouse associate")
    parser.add_argument("--state", default=None,
                        help="two-letter state code; nationwide when omitted")
    parser.add_argument("--hours-old", type=int, default=336)
    parser.add_argument("--json", dest="json_path", default=None)
    parser.add_argument("--contact-sample", type=int, default=3,
                        help="companies to probe for contact-page emails")
    parser.add_argument("--skip-contacts", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    now = datetime.now(timezone.utc)
    access = AccessLayer(settings=settings)  # shared: one limiter, one budget
    rows: list[dict] = []
    all_jobs: list[RawJob] = []
    started = time.monotonic()

    # ---- Every enabled registry source, one query each ---------------------
    from nexbase.discovery.planner import NATIONWIDE, US_STATES, TitleVariant
    from nexbase.discovery.registry import FAILED, SourceQuery, build_registry

    geo = next((g for g in US_STATES if g.code == (args.state or "").upper()), NATIONWIDE)
    query = SourceQuery(TitleVariant(args.term, "INPUT"), geo, "", args.hours_old)
    for source in build_registry(settings, access=access).sources():
        if not source.enabled:
            continue
        t0 = time.monotonic()
        error = None
        jobs: list[RawJob] = []
        try:
            jobs, outcome = source.adapter(query)
            if outcome.stop_reason in FAILED:
                error = outcome.error_message or outcome.stop_reason
        except Exception as exc:  # reported, never worked around
            error = f"{type(exc).__name__}: {exc}"
        rows.append(_source_row(source.id, jobs, error,
                                time.monotonic() - t0, settings, now))
        all_jobs.extend(jobs)

    # ---- Contact-page emails on a bounded sample --------------------------
    # Discovery only: this reads public company pages, it never enriches,
    # verifies or queues anything.
    contact_page = {"companies_sampled": 0, "pages_fetched": 0,
                    "pages_blocked": 0, "emails": [], "named_contacts": 0}
    if not args.skip_contacts:
        from nexbase.contacts.discovery import ContactDiscovery

        with_site = [c for c in Deduplicator().dedupe(
            Normalizer().normalize(all_jobs)[0]) if c.company_website][:args.contact_sample]
        discovery = ContactDiscovery(access=access, settings=settings)
        for company in with_site:
            contact_page["companies_sampled"] += 1
            try:
                found = discovery.discover(
                    company.company_name_normalized, company.company_website, [], []
                )
            except Exception as exc:
                contact_page.setdefault("errors", []).append(str(exc))
                continue
            contact_page["pages_fetched"] += found.pages_fetched
            contact_page["pages_blocked"] += found.pages_blocked
            contact_page["emails"].extend(found.page_emails)
            contact_page["named_contacts"] += sum(
                1 for c in found.candidates if c.name and c.email
            )
        contact_page["emails"] = sorted(set(contact_page["emails"]))

    # ---- Overlap across sources ------------------------------------------
    normalized, discarded = Normalizer().normalize(all_jobs)
    companies = Deduplicator().dedupe(normalized)
    kept = sum(c.hiring_intensity for c in companies)

    report = {
        "generated_at": now.isoformat(),
        "query": {"term": args.term, "location": geo.name,
                  "hours_old": args.hours_old},
        "totals": {
            "jobs_returned": len(all_jobs),
            "normalized": len(normalized),
            "discarded_no_company_name": len(discarded),
            "companies": len(companies),
            "jobs_after_dedup": kept,
            "duplicate_rate": (
                round(1 - kept / len(normalized), 3) if normalized else 0.0
            ),
            "companies_with_domain": sum(1 for c in companies if c.domain),
            "raw_observed_emails": sorted(
                {e for c in companies for e in c.observed_emails}
            ),
        },
        "contact_page_discovery": contact_page,
        "access": {
            "scrapling_pages": access.pages_fetched,
            "camoufox_fallbacks": access.fallback_count,
            "pages_blocked": access.pages_blocked,
            "robots_blocked": access.robots_blocked,
            "ssrf_blocked": access.ssrf_blocked,
            "camoufox_budget": settings.camoufox_budget_per_run,
        },
        "sources": rows,
        "runtime_seconds": round(time.monotonic() - started, 2),
        "note": "Discovery only. No enrichment, no verification, no outreach.",
    }

    print(json.dumps(report, indent=2))
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nwritten: {args.json_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
