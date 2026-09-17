"""Load test for the Direct + ATS source layer. Discovery only.

Runs a real planner plan through the real source registry and measures, per
source: requests, jobs, fresh jobs, unique companies, runtime, success and failure
rates, timeouts, 429s, 403s, CAPTCHA / Cloudflare, connection and parse errors,
memory, and silent empty results. It also checks run integrity: duplicate planner
slices, duplicate requests, duplicate RawJobs, results mixed between sources,
source metrics that disagree with the jobs actually returned, and worker threads
left running.

Nothing is bypassed: robots.txt, CAPTCHA and Cloudflare handling are the access
layer's own. Apify sources are never run. No qualification, contacts, enrichment
or verification.

    python scripts/load_test_sources.py --label r1 --sector Manufacturing \\
        --job "Warehouse Manager" --variants 1 --calls 4 --jobspy-results 150
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psutil  # noqa: E402

from nexbase.config import get_settings  # noqa: E402

HOST_SOURCE = {
    "indeed.com": "direct:indeed", "linkedin.com": "direct:linkedin",
    "simplyhired.com": "direct:simplyhired", "talent.com": "direct:talent_com",
    "postjobfree.com": "direct:postjobfree",
}
SCOPE = ("indeed", "linkedin", "simplyhired", "talent_com", "postjobfree", "greenhouse",
         "lever", "ashby", "workable", "smartrecruiters", "bamboohr", "breezy", "jazzhr",
         "recruitee", "paylocity")


def source_of_url(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower()
    if host == "stapply.ai" or host.endswith(".stapply.ai"):
        return "ats:dataset"
    for suffix, source in HOST_SOURCE.items():
        if host == suffix or host.endswith("." + suffix):
            return source
    return None


class RequestLog:
    """Every outbound HTTP request the scrapers make, by source, with its status."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rows: list[dict] = []

    def add(self, url, status, error=None, seconds=0.0, engine="", body=None):
        with self.lock:
            key = url if body is None else f"{url} {json.dumps(body, sort_keys=True, default=str)}"
            start = body.get("start") if isinstance(body, dict) else None
            self.rows.append({"source": source_of_url(url), "url": url, "key": key,
                              "status": status, "start": start,
                              "error": error, "seconds": round(seconds, 2), "engine": engine,
                              "thread": threading.current_thread().name})

    def install(self):
        import requests
        import tls_client

        from nexbase.access import fetcher

        log = self
        original_scrapling = fetcher.AccessLayer._scrapling_get

        def scrapling_get(layer, url, alternate=False):
            started = time.monotonic()
            try:
                page = original_scrapling(layer, url, alternate)
            except Exception as exc:
                log.add(url, None, f"{type(exc).__name__}: {exc}"[:300],
                        time.monotonic() - started, "scrapling")
                raise
            log.add(url, getattr(page, "status", None), None, time.monotonic() - started,
                    "scrapling-alt" if alternate else "scrapling")
            return page

        fetcher.AccessLayer._scrapling_get = scrapling_get

        original_tls = tls_client.Session.execute_request

        def tls_request(session, method, url, *args, **kwargs):
            started = time.monotonic()
            try:
                response = original_tls(session, method, url, *args, **kwargs)
            except Exception as exc:
                log.add(url, None, f"{type(exc).__name__}: {exc}"[:300],
                        time.monotonic() - started, "tls_client")
                raise
            log.add(url, response.status_code, None, time.monotonic() - started, "tls_client",
                    body=kwargs.get("params") or kwargs.get("json") or kwargs.get("data"))
            return response

        tls_client.Session.execute_request = tls_request

        original_requests = requests.Session.request

        def requests_request(session, method, url, *args, **kwargs):
            started = time.monotonic()
            try:
                response = original_requests(session, method, url, *args, **kwargs)
            except Exception as exc:
                log.add(url, None, f"{type(exc).__name__}: {exc}"[:300],
                        time.monotonic() - started, "requests")
                raise
            log.add(url, response.status_code, None, time.monotonic() - started, "requests",
                    body=kwargs.get("params") or kwargs.get("json") or kwargs.get("data"))
            return response

        requests.Session.request = requests_request

        import httpx

        original_send = httpx.Client.send

        def httpx_send(client, request, *args, **kwargs):
            started = time.monotonic()
            try:
                response = original_send(client, request, *args, **kwargs)
            except Exception as exc:
                log.add(str(request.url), None, f"{type(exc).__name__}: {exc}"[:300],
                        time.monotonic() - started, "httpx")
                raise
            log.add(str(request.url), response.status_code, None,
                    time.monotonic() - started, "httpx")
            return response

        httpx.Client.send = httpx_send


class ResourceSampler(threading.Thread):
    def __init__(self, interval=0.5):
        super().__init__(daemon=True, name="resource-sampler")
        self.interval = interval
        self.proc = psutil.Process()
        self.stop_flag = threading.Event()
        self.peak_rss = 0
        self.peak_threads = 0
        self.peak_connections = 0

    def run(self):
        while not self.stop_flag.is_set():
            try:
                self.peak_rss = max(self.peak_rss, self.proc.memory_info().rss)
                self.peak_threads = max(self.peak_threads, self.proc.num_threads())
                self.peak_connections = max(self.peak_connections,
                                            len(self.proc.net_connections(kind="inet")))
            except Exception:
                pass
            self.stop_flag.wait(self.interval)


def failure_class(outcome) -> str:
    """EMPTY_RESULT | RATE_LIMITED | BLOCKED | TIMEOUT | PARSE_FAILURE | FETCH_FAILURE | OK."""
    reason = outcome.stop_reason
    text = f"{outcome.error_type or ''} {outcome.error_message or ''}".lower()
    if reason == "RATE_LIMITED" or "429" in text or "too many requests" in text:
        return "RATE_LIMITED"
    if reason in ("BLOCKED", "ROBOTS_RESTRICTED"):
        return "BLOCKED"
    if reason == "ERROR":
        if "timeout" in text or "timed out" in text:
            return "TIMEOUT"
        if "parse" in text:
            return "PARSE_FAILURE"
        return "FETCH_FAILURE"
    if outcome.jobs_returned == 0:
        return "EMPTY_RESULT"
    return "OK"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--sector", default="Manufacturing")
    parser.add_argument("--job", default="Warehouse Manager")
    parser.add_argument("--variants", type=int, default=1)
    parser.add_argument("--calls", type=int, default=4, help="max queries per source per run")
    parser.add_argument("--workers", type=int, default=1, help="sources run in parallel")
    parser.add_argument("--jobspy-results", type=int, default=50)
    parser.add_argument("--pages", type=int, default=None)
    parser.add_argument("--results", type=int, default=None)
    parser.add_argument("--sources", default=",".join(SCOPE))
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    settings = get_settings()  # the shared instance board scrapers also read
    settings.planner_max_title_variants = args.variants
    settings.source_max_calls_per_run = args.calls
    settings.jobspy_results_wanted = args.jobspy_results
    if args.pages:
        settings.discovery_max_pages_per_query = args.pages
    if args.results:
        settings.discovery_max_results_per_query = args.results
    wanted = set(args.sources.split(","))
    settings.sources_disabled = ",".join(p for p in SCOPE if p not in wanted)
    if hasattr(settings, "source_max_workers"):
        settings.source_max_workers = args.workers
    elif args.workers > 1:
        print("source_max_workers is not supported by this build", file=sys.stderr)
        return 2

    from nexbase.access.fetcher import AccessLayer
    from nexbase.discovery.planner import DiscoveryPlanner
    from nexbase.discovery.registry import build_registry
    from nexbase.pipeline.freshness import FreshnessFilter
    from nexbase.pipeline.normalize import Normalizer

    requests_log = RequestLog()
    requests_log.install()
    sampler = ResourceSampler()
    threads_before = {t.name for t in threading.enumerate()}
    rss_before = psutil.Process().memory_info().rss
    sampler.start()

    plan = DiscoveryPlanner(settings).plan(args.sector, args.job)
    access = AccessLayer(settings=settings)
    registry = build_registry(settings, access=access)
    started = time.monotonic()
    crashed = None
    try:
        result = registry.run(plan)
    except Exception as exc:  # the run itself must never die
        crashed = f"{type(exc).__name__}: {exc}"
        result = None
    runtime = time.monotonic() - started
    time.sleep(1.0)
    sampler.stop_flag.set()
    sampler.join()
    rss_after = psutil.Process().memory_info().rss
    lingering = sorted({t.name for t in threading.enumerate() if t.is_alive()}
                       - threads_before - {"resource-sampler"})

    now = datetime.now(timezone.utc)
    report: dict = {
        "label": args.label, "generated_at": now.isoformat(),
        "load": {k: v for k, v in vars(args).items() if k not in ("out", "label")},
        "plan": {"titles": [t.title for t in plan.titles], "hours_old": plan.hours_old,
                 "geo_partitions": len(plan.geo_partitions)},
        "runtime_seconds": round(runtime, 1), "crashed": crashed,
        "resources": {"rss_mb_before": round(rss_before / 2**20),
                      "rss_mb_peak": round(sampler.peak_rss / 2**20),
                      "rss_mb_after": round(rss_after / 2**20),
                      "peak_threads": sampler.peak_threads,
                      "peak_inet_connections": sampler.peak_connections,
                      "lingering_threads": lingering},
        "access": {"pages_fetched": access.pages_fetched, "pages_blocked": access.pages_blocked,
                   "rate_limited": access.rate_limited, "robots_blocked": access.robots_blocked,
                   "camoufox_fallbacks": access.fallback_count},
    }
    if result is None:
        print(json.dumps(report, indent=2))
        return 1

    jobs = result.jobs
    by_source_jobs: dict[str, list] = defaultdict(list)
    for job in jobs:
        by_source_jobs[job.provenance.get("source_id")].append(job)

    normalizer = Normalizer()
    freshness = FreshnessFilter(settings)
    outcomes_by_source: dict[str, list] = defaultdict(list)
    for outcome in result.coverage.outcomes:
        outcomes_by_source[outcome.source].append(outcome)

    requests_by_source: dict[str, list] = defaultdict(list)
    for row in requests_log.rows:
        requests_by_source[row["source"]].append(row)

    integrity: dict = {"duplicate_slices": {}, "duplicate_rawjobs": {}, "mixed_sources": {},
                       "metric_mismatch": {}, "duplicate_requests": {}}
    sources = {}
    for source_id, status in result.source_status.items():
        if status.get("outcome") == "DISABLED":
            continue
        source_outcomes = [o for o in outcomes_by_source[source_id] if o.query]
        source_jobs = by_source_jobs.get(source_id, [])
        normalized, screened = normalizer.normalize(source_jobs)
        fresh = [n for n in normalized if freshness.evaluate(n, now=now).keep]
        classes = Counter(failure_class(o) for o in source_outcomes)
        reqs = requests_by_source.get(source_id, [])
        statuses = Counter(str(r["status"]) for r in reqs)
        req_errors = [r["error"] for r in reqs if r["error"]]
        blocked_msgs = [o.error_message for o in source_outcomes
                        if o.error_message and any(k in o.error_message.lower() for k in
                                                   ("captcha", "cloudflare", "challenge"))]

        slices = Counter((o.query, o.location_scope) for o in source_outcomes)
        dup_slices = {f"{q}|{g}": n for (q, g), n in slices.items() if n > 1}
        if dup_slices:
            integrity["duplicate_slices"][source_id] = dup_slices
        keys = Counter(j.external_id or j.application_url for j in source_jobs)
        dups = {k: n for k, n in keys.items() if n > 1}
        if dups:
            integrity["duplicate_rawjobs"][source_id] = len(dups)
        mixed = [j.source_site for j in source_jobs if j.source_site != status["portal"]]
        if mixed:
            integrity["mixed_sources"][source_id] = Counter(mixed).most_common(3)
        if status.get("jobs_accepted") != len(source_jobs):
            integrity["metric_mismatch"][source_id] = {
                "stats_accepted": status.get("jobs_accepted"), "jobs_seen": len(source_jobs)}
        search_urls = Counter(r["key"] for r in reqs if r["engine"] != "scrapling-alt")
        dup_urls = {u: n for u, n in search_urls.items() if n > 1 and "robots.txt" not in u}
        if dup_urls:
            integrity["duplicate_requests"][source_id] = {
                "count": sum(n - 1 for n in dup_urls.values()),
                "samples": [k[:160] for k, _ in Counter(dup_urls).most_common(3)]}

        offsets = [r["start"] for r in reqs if r.get("start") is not None]
        calls = len(source_outcomes)
        failed = sum(classes[c] for c in
                     ("RATE_LIMITED", "BLOCKED", "TIMEOUT", "PARSE_FAILURE", "FETCH_FAILURE"))
        sources[source_id] = {
            "queries": calls,
            "http_requests": len(reqs) if not source_id.startswith("ats:") else None,
            "jobs_returned": status.get("jobs_returned"),
            "jobs_accepted": len(source_jobs),
            "schema_rejected": status.get("schema_rejected"),
            "fresh": len(fresh),
            "screened": len(screened),
            "unique_companies": len({n.company_name_normalized for n in normalized}),
            "runtime_seconds": round((status.get("duration_ms") or 0) / 1000, 1),
            "success_rate": round((calls - failed) / calls, 3) if calls else None,
            "failure_rate": round(failed / calls, 3) if calls else None,
            "classes": dict(classes),
            "stop_reasons": status.get("stop_reasons"),
            "http_status": dict(statuses),
            "http_429": statuses.get("429", 0), "http_403": statuses.get("403", 0),
            "timeouts": sum(1 for e in req_errors if "timeout" in e.lower() or
                            "timed out" in e.lower()) + classes.get("TIMEOUT", 0),
            "connection_errors": sum(1 for e in req_errors if "timeout" not in e.lower()),
            "captcha_cloudflare": len(blocked_msgs),
            "parse_errors": classes.get("PARSE_FAILURE", 0),
            "silent_empty": sum(1 for o in source_outcomes if o.jobs_returned == 0 and
                                o.stop_reason not in ("SOURCE_EXHAUSTED",) and
                                not o.error_message),
            "skipped_queries": status.get("skipped_queries"),
            "fanouts": status.get("fanouts"),
            "last_error": status.get("last_error"),
            "request_error_samples": req_errors[:3],
            "linkedin_start_offsets": offsets[:40] if offsets else None,
        }
    report["sources"] = sources
    report["integrity"] = integrity
    dataset = requests_by_source.get("ats:dataset", [])
    report["ats_dataset_requests"] = {
        "total": len(dataset),
        "by_url": dict(Counter(r["url"].split("?")[0] for r in dataset).most_common(10)),
        "unattributed_hosts": dict(Counter((urlsplit(r["url"]).hostname or "")
                                           for r in requests_by_source.get(None, []))
                                   .most_common(10)),
    }
    report["totals"] = {"jobs": len(jobs), "requests": len(requests_log.rows),
                        "requests_by_thread": dict(Counter(r["thread"] for r in
                                                           requests_log.rows).most_common(8))}
    out = Path(args.out or f"reports/load_test/{args.label}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("label", "runtime_seconds", "crashed",
                                             "resources", "integrity")}, indent=2))
    for sid, row in sources.items():
        print(f"{sid:22} q={row['queries']:3} req={row['http_requests']} "
              f"jobs={row['jobs_accepted']:5} fresh={row['fresh']:5} "
              f"co={row['unique_companies']:4} {row['runtime_seconds']:7}s "
              f"ok={row['success_rate']} {row['classes']} http={row['http_status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
