"""Apify actors for portals NexBase cannot scrape reliably itself.

Live checks on 2026-09-16 (``SourceClass.DIRECT`` via JobSpy): ZipRecruiter
answered 403 (Cloudflare), Glassdoor 400 "location not parsed", for both a
nationwide and a state query. Those two portals are served by Apify actors
instead. Google Jobs was not moved: no actor found documents both a job title
and a posting date in its output, and it re-lists boards already covered.

Actors were chosen for a *documented* output schema that includes a posting
date, read from each actor's public build definition. Field names below follow
those schemas. No Apify token was available, so no actor has been run by
NexBase yet: set ``APIFY_API_TOKEN`` and verify with ``scripts/live_smoke.py``.

Apify is paid per result. Every call is capped three ways: items per call,
dollars per call, and calls per run (``APIFY_MAX_*`` settings).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx
from bs4 import BeautifulSoup

from nexbase.core.models import RawJob
from nexbase.core.source_tracking import JOB_BOARD
from nexbase.core.timeutils import coerce_datetime

APIFY_API = "https://api.apify.com/v2"


@dataclass(frozen=True)
class ApifyActor:
    actor_id: str
    #: (title, geo, hours_old, max_items) -> actor input
    build_input: Callable[..., dict]
    #: (item, portal) -> RawJob, or None when the item is not a job
    to_raw_job: Callable[[dict, str], RawJob | None]
    #: hours_old -> the date window the actor enforces, in hours
    date_window_hours: Callable[[int], int | None]


def run_actor(actor_id: str, actor_input: dict, *, token: str, max_items: int,
              max_charge_usd: float, timeout_seconds: int) -> list[dict]:
    """Run an actor synchronously and return its dataset items.

    The token travels in a header, never in the URL. ``maxItems`` and
    ``maxTotalChargeUsd`` cap what Apify may bill for this one run.
    """
    response = httpx.post(
        f"{APIFY_API}/acts/{actor_id}/run-sync-get-dataset-items",
        params={"timeout": timeout_seconds, "maxItems": max_items,
                "maxTotalChargeUsd": max_charge_usd, "format": "json"},
        headers={"Authorization": f"Bearer {token}"},
        json=actor_input,
        timeout=timeout_seconds + 30,
    )
    response.raise_for_status()
    items = response.json()
    if not isinstance(items, list):
        raise ValueError(f"Apify {actor_id} returned {type(items).__name__}, not a list")
    return items[:max_items]


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    return text or None


def _raw_job(portal: str, **fields) -> RawJob:
    return RawJob(source_type=JOB_BOARD.source_type.value,
                  source_priority=int(JOB_BOARD.source_priority.value),
                  source_site=portal, **fields)


# ---------------------------------------------------------------------------
# Glassdoor: agentx~glassdoor-jobs-scraper (41-field documented dataset)
# ---------------------------------------------------------------------------
def _glassdoor_input(title, geo, hours_old, max_items) -> dict:
    data = {"keyword": title, "country": "United States", "max_results": max_items}
    if geo.level == "STATE":
        data["location"] = geo.name
    if hours_old:
        since = datetime.now(timezone.utc) - timedelta(hours=hours_old)
        data["posted_since"] = since.strftime("%Y-%m-%d")
    return data


def _glassdoor_job(item: dict, portal: str) -> RawJob | None:
    if not item.get("title"):
        return None
    location = item.get("location") if isinstance(item.get("location"), dict) else {}
    return _raw_job(
        portal,
        external_id=item.get("platform_url"),
        title=item.get("title"),
        company_name=item.get("company_name"),
        company_url=item.get("company_url"),
        company_website=item.get("company_website"),
        location=location.get("raw"),
        country=location.get("country_code"),
        description=_text(item.get("description")),
        posted_at=coerce_datetime(item.get("posted_date")),
        application_url=item.get("platform_url"),
        apply_url=item.get("official_url") or item.get("platform_url"),
        employment_type=item.get("job_type"),
        is_remote=item.get("is_remote"),
        company_industry=item.get("company_industry"),
        company_employee_count=item.get("employee_count"),
        company_revenue=item.get("company_revenue"),
        company_addresses=item.get("company_addresses"),
        applicant_count=item.get("applicant_count"),
        observed_emails=[str(e) for e in (item.get("emails") or []) if e],
        raw={k: v for k, v in item.items() if k != "description"},
    )


# ---------------------------------------------------------------------------
# ZipRecruiter: silentflow~ziprecruiter-scraper-ppe (documented example record)
# ---------------------------------------------------------------------------
#: The actor's fixed postedWithin windows, in hours.
_ZIPRECRUITER_WINDOWS = (("today", 24), ("3days", 72), ("week", 168), ("2weeks", 336),
                         ("month", 744))


def _ziprecruiter_window(hours_old):
    return next(((name, hours) for name, hours in _ZIPRECRUITER_WINDOWS if hours_old <= hours),
                _ZIPRECRUITER_WINDOWS[-1])


def _ziprecruiter_input(title, geo, hours_old, max_items) -> dict:
    data = {"searches": [title], "sort": "date", "maxItems": max_items,
            "includeDetails": True}
    if geo.level == "STATE":
        data["location"] = geo.name
    if hours_old:
        # The actor's windows are fixed; the freshness filter trims the rest.
        data["postedWithin"] = _ziprecruiter_window(hours_old)[0]
    return data


def _ziprecruiter_job(item: dict, portal: str) -> RawJob | None:
    if item.get("dataType", "job") != "job" or not item.get("title"):
        return None
    return _raw_job(
        portal,
        external_id=str(item["id"]) if item.get("id") else item.get("url"),
        title=item.get("title"),
        company_name=item.get("company"),
        company_website=item.get("companyUrl"),
        location=item.get("location"),
        country=item.get("country"),
        description=_text(item.get("description")),
        posted_at=coerce_datetime(item.get("postedDate")),
        application_url=item.get("url"),
        apply_url=item.get("applyUrl") or item.get("url"),
        employment_type=item.get("employmentType"),
        company_industry=item.get("industry"),
        raw={k: v for k, v in item.items() if k != "description"},
    )


#: Portal -> the actor that serves it.
APIFY_ACTORS: dict[str, ApifyActor] = {
    # posted_since is a date, so the window can reach back to that day's start.
    "glassdoor": ApifyActor("agentx~glassdoor-jobs-scraper", _glassdoor_input, _glassdoor_job,
                            lambda hours_old: hours_old + 24 if hours_old else None),
    "zip_recruiter": ApifyActor("silentflow~ziprecruiter-scraper-ppe",
                                _ziprecruiter_input, _ziprecruiter_job,
                                lambda hours_old: _ziprecruiter_window(hours_old)[1] if hours_old else None),
}
