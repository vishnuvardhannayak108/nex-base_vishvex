"""Compare candidate Apify enrichment actors. DOES NOT RUN without client credentials.

    python scripts/benchmark_apify_actors.py \
        --companies benchmarks/apify/companies.json \
        --actors benchmarks/apify/actors.py \
        --out reports/apify_actor_benchmark.json

``--companies`` is a JSON list like ``benchmarks/apify/companies.example.json``
(10-20 real NexBase-style companies with their verified domains; add
``known_people`` with LinkedIn profile URLs to benchmark profile_to_email).

``--actors`` is a Python file defining ``ACTORS: list[ApifyEnrichmentActor]``,
written only after each candidate's documented input/output schema has been
reviewed (see ``benchmarks/apify/actors_template.py``). Actors defined there are
benchmarked only; nothing is registered for the enrichment waterfall.

Every run is paid and capped by ``APIFY_MAX_CHARGE_USD_PER_CALL``. The script
refuses to run without ``APIFY_API_TOKEN`` (live validation is pending client
credentials), without actor definitions, or without ``--confirm-paid-runs``.
Actors that scrape LinkedIn are reported with a risk that needs client/business
approval before production use.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nexbase.config import get_settings  # noqa: E402
from nexbase.enrichment.apify_benchmark import load_companies, run_benchmark  # noqa: E402

PENDING = 2


def load_actors(path: str):
    spec = importlib.util.spec_from_file_location("benchmark_actors", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return list(getattr(module, "ACTORS", []))


def main(argv: list[str] | None = None, settings=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--companies", required=True)
    parser.add_argument("--actors", required=True)
    parser.add_argument("--out", default="reports/apify_actor_benchmark.json")
    parser.add_argument("--max-items", type=int, default=25)
    parser.add_argument("--confirm-paid-runs", action="store_true",
                        help="required: every actor run is billed by Apify")
    args = parser.parse_args(argv)
    settings = settings or get_settings()

    if not settings.apify_api_token:
        print("PENDING CLIENT CREDENTIALS: APIFY_API_TOKEN is not set; nothing was run.",
              file=sys.stderr)
        return PENDING
    actors = load_actors(args.actors)
    if not actors:
        print("No actor definitions in --actors; nothing was run.", file=sys.stderr)
        return PENDING
    if not args.confirm_paid_runs:
        print("Refusing to start paid actor runs without --confirm-paid-runs.", file=sys.stderr)
        return PENDING

    companies = load_companies(args.companies)
    report = run_benchmark(actors, companies, settings, max_items=args.max_items)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
