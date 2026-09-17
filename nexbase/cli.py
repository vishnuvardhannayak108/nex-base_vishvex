"""NexBase command line interface.

    python -m nexbase.cli db apply-schema
    python -m nexbase.cli db health
    python -m nexbase.cli sectors --sector Manufacturing
    python -m nexbase.cli sources
    python -m nexbase.cli plan --sector Manufacturing --job "Warehouse Manager"
    python -m nexbase.cli run --sector Manufacturing --job "Warehouse Manager"
    python -m nexbase.cli export leads.csv
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from nexbase.config import get_settings
from nexbase.logging_setup import ensure_logging_configured


def _repo():
    from nexbase.db.repository import SupabaseRepository

    return SupabaseRepository()


# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------
def cmd_db_apply_schema(args) -> int:
    from nexbase.db.schema_loader import apply_schema

    try:
        asyncio.run(apply_schema(args.dsn))
    except Exception as exc:
        print(f"schema apply failed: {exc}", file=sys.stderr)
        return 1
    print("schema applied")
    return cmd_db_health(args)


def cmd_db_health(args) -> int:
    health = _repo().health_check()
    print(json.dumps(health, indent=2))
    if not health["configured"]:
        print("\nSupabase is not configured (set SUPABASE_URL and "
              "SUPABASE_SERVICE_ROLE_KEY).", file=sys.stderr)
        return 1
    if not health["schema_applied"]:
        print(
            f"\nMissing tables: {', '.join(health['missing'])}\n"
            "Run: python -m nexbase.cli db apply-schema",
            file=sys.stderr,
        )
        return 1
    return 0


# ---------------------------------------------------------------------------
# sectors / sources / plan / run
# ---------------------------------------------------------------------------
def cmd_sectors(args) -> int:
    """Print the sector list, and suggested jobs for one sector."""
    from nexbase.discovery import taxonomy
    from nexbase.discovery.planner import known_sectors

    payload = {"sectors": known_sectors()}
    if args.sector:
        payload["sector"] = args.sector
        payload["suggested_jobs"] = taxonomy.suggest_terms_for_sector(
            args.sector, count=args.limit)
    print(json.dumps(payload, indent=2))
    return 0


def cmd_sources(args) -> int:
    """Print every registered source and whether it will run."""
    from nexbase.discovery.registry import build_registry

    print(json.dumps([s.describe() for s in build_registry().sources()], indent=2))
    return 0


def _plan(args):
    from nexbase.core.errors import DiscoveryError
    from nexbase.discovery.planner import DiscoveryPlanner

    try:
        return DiscoveryPlanner().plan(args.sector, args.job)
    except DiscoveryError as exc:
        print(str(exc), file=sys.stderr)
        return None


def cmd_plan(args) -> int:
    """Print the USA-wide plan for a sector and job without running it."""
    plan = _plan(args)
    if plan is None:
        return 1
    print(json.dumps(plan.to_dict(), indent=2))
    return 0


def cmd_run(args) -> int:
    from nexbase.pipeline.runner import PipelineRunner

    plan = _plan(args)
    if plan is None:
        return 1
    print(f"plan {plan.run_key}: {len(plan.titles)} title(s), USA-wide",
          file=sys.stderr)
    report = PipelineRunner().run(
        plan=plan,
        persist=not args.dry_run,
        stop_at=args.stop_at,
        enrich_linkedin_signal=args.linkedin_signal,
    )
    print(json.dumps(report.to_dict(include_records=args.verbose), indent=2))
    return 0


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
def cmd_export(args) -> int:
    from nexbase.export import export_leads_csv

    count = export_leads_csv(args.path, repo=_repo(), status=args.status,
                             lead_status_filter=args.lead_status)
    print(f"wrote {count} rows to {args.path}")
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nexbase", description=__doc__)
    parser.add_argument("--log-level", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    db = sub.add_parser("db", help="database maintenance").add_subparsers(
        dest="db_command", required=True
    )
    apply_p = db.add_parser("apply-schema", help="apply schema.sql to Supabase")
    apply_p.add_argument("--dsn", default=None, help="override SUPABASE_DB_URL")
    apply_p.set_defaults(func=cmd_db_apply_schema)
    health_p = db.add_parser("health", help="verify every table exists")
    health_p.set_defaults(func=cmd_db_health)

    sectors_p = sub.add_parser("sectors", help="list sectors and suggested jobs")
    sectors_p.add_argument("--sector", default=None,
                           help="show suggested jobs for this sector")
    sectors_p.add_argument("--limit", type=int, default=50)
    sectors_p.set_defaults(func=cmd_sectors)

    sub.add_parser("sources", help="list registered sources").set_defaults(
        func=cmd_sources)

    def sector_and_job(p):
        p.add_argument("--sector", required=True, help="one of: nexbase sectors")
        p.add_argument("--job", required=True, help="job title or job category")

    plan_p = sub.add_parser("plan", help="show the USA-wide plan, run nothing")
    sector_and_job(plan_p)
    plan_p.set_defaults(func=cmd_plan)

    run_p = sub.add_parser("run", help="run the pipeline for a sector and job")
    sector_and_job(run_p)
    run_p.add_argument("--dry-run", action="store_true", help="do not write to the database")
    run_p.add_argument("--stop-at", choices=["before_contacts", "before_enrichment"],
                       default=None)
    run_p.add_argument("--linkedin-signal", action="store_true")
    run_p.add_argument("-v", "--verbose", action="store_true")
    run_p.set_defaults(func=cmd_run)

    exp = sub.add_parser("export", help="export final leads to CSV (no sending)")
    exp.add_argument("path")
    exp.add_argument("--status", default="QUALIFIED")
    exp.add_argument("--lead-status", default=None,
                     choices=["READY", "PENDING_VERIFICATION", "NO_VERIFIED_POC_EMAIL"])
    exp.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ensure_logging_configured(args.log_level or get_settings().log_level)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
