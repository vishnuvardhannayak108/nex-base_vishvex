"""NexBase command line interface.

    python -m nexbase.cli db apply-schema
    python -m nexbase.cli db health
    python -m nexbase.cli sectors --sector Manufacturing
    python -m nexbase.cli run --sector Manufacturing --term "Warehouse Manager"
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
# sectors / run
# ---------------------------------------------------------------------------
def cmd_sectors(args) -> int:
    """Print the sector list, and suggested search terms for one sector.

    Suggestions only: `run` searches the terms you pass it, not these.
    """
    from nexbase.discovery import taxonomy
    from nexbase.discovery.planner import NATIONWIDE, known_sectors

    payload = {"sectors": known_sectors(), "default_location": NATIONWIDE}
    if args.sector:
        payload["sector"] = args.sector
        payload["suggested_search_terms"] = taxonomy.suggest_terms_for_sector(
            args.sector, count=args.limit)
    print(json.dumps(payload, indent=2))
    return 0


def cmd_run(args) -> int:
    from nexbase.core.errors import DiscoveryError
    from nexbase.discovery.planner import DiscoveryPlanner, build_config
    from nexbase.pipeline.runner import PipelineRunner

    settings = get_settings()

    # Optional per-run override of the configured 11-200 employee band.
    overrides = {}
    if args.min_employees is not None:
        overrides["size_filter_min"] = args.min_employees
    if args.max_employees is not None:
        overrides["size_filter_max"] = args.max_employees
    if overrides:
        settings = settings.model_copy(update=overrides)
        if settings.size_filter_min > settings.size_filter_max:
            print("--min-employees must not exceed --max-employees", file=sys.stderr)
            return 1
        print(
            f"employee band for this run: "
            f"{settings.size_filter_min}-{settings.size_filter_max}",
            file=sys.stderr,
        )

    # Discovery is whatever the operator asked for. There is no path here
    # that invents a sector, a location or a search term.
    plan = None
    terms = [t.strip() for t in (args.terms or []) if t.strip()]
    if terms:
        try:
            config = build_config(
                search_terms=terms,
                sector=args.sector,
                location=args.location,
                size_min=args.min_employees,
                size_max=args.max_employees,
                freshness_days=args.freshness_days,
            )
        except DiscoveryError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        plan = DiscoveryPlanner(settings).plan(config)
        print(
            f"plan: {len(plan.probes)} probe(s) | sector={config.sector or '-'} "
            f"| location={config.location}"
            f"{' (nationwide)' if config.nationwide else ''}",
            file=sys.stderr,
        )

    runner = PipelineRunner(settings)
    report = runner.run(
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

    count = export_leads_csv(args.path, repo=_repo(), status=args.status)
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

    plan_p = sub.add_parser(
        "sectors", help="list sectors and suggested search terms")
    plan_p.add_argument("--sector", default=None,
                        help="show suggested search terms for this sector")
    plan_p.add_argument("--limit", type=int, default=50)
    plan_p.set_defaults(func=cmd_sectors)

    run_p = sub.add_parser("run", help="run the pipeline")
    run_p.add_argument(
        "--sector", default=None,
        help="sector for this run only (see `nexbase sectors`)")
    run_p.add_argument(
        "--location", default=None,
        help="USA (default) means nationwide; or name a state/metro to narrow")
    run_p.add_argument(
        "--term", dest="terms", action="append", default=None,
        help="a search term; repeat for more. Required to discover anything.")
    run_p.add_argument(
        "--freshness-days", type=int, default=None,
        help="maximum posting age; the brief's ceiling is 14")
    run_p.add_argument(
        "--min-employees", type=int, default=None,
        help="Employee-band minimum for this run (default: configured value).")
    run_p.add_argument(
        "--max-employees", type=int, default=None,
        help="Employee-band maximum for this run (default: configured value).")
    run_p.add_argument("--dry-run", action="store_true", help="do not write to the database")
    run_p.add_argument(
        "--stop-at",
        choices=["before_contacts"],
        default=None,
    )
    run_p.add_argument("--linkedin-signal", action="store_true")
    run_p.add_argument("-v", "--verbose", action="store_true")
    run_p.set_defaults(func=cmd_run)

    exp = sub.add_parser("export", help="export leads to CSV")
    exp.add_argument("path")
    exp.add_argument("--status", default="QUALIFIED")
    exp.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ensure_logging_configured(args.log_level or get_settings().log_level)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
