"""ZoomInfo provider: the documented ZoomInfo Enterprise API (legacy), paid.

Every request and response field used here is taken from ZoomInfo's published
Enterprise API documentation (api-docs.zoominfo.com, read 2026-09-17):

* ``POST /authenticate`` with ``{"username", "password"}`` returns ``{"jwt"}``,
  valid 60 minutes; refreshed after 55. (Client ID + private key authentication
  needs ZoomInfo's own signing library and is not implemented; a pre-issued JWT
  may be supplied instead.)
* ``POST /enrich/company`` with ``matchCompanyInput`` (``companyWebsite``,
  ``companyName``) and ``outputFields``; response ``data.result[].data[]``.
* ``POST /search/contact`` with ``companyId`` and ``jobTitle`` ("use OR to input
  multiple job titles"); results carry ``id``, ``firstName``, ``lastName``,
  ``jobTitle``, ``contactAccuracyScore``, ``hasEmail`` and ``company{id,name}``,
  and never an email.
* ``POST /enrich/contact`` with ``matchPersonInput`` (``personId``, at most 25
  per request) and ``outputFields``; response ``data.result[].data[]`` with
  ``email``, ``jobTitle``, ``externalUrls[{type,url}]``, ``contactAccuracyScore``.

The documentation marks this API as being deprecated in favour of ZoomInfo's
new OAuth API, whose reference pages could not be read (bot challenge), so it is
not implemented. Nothing here has been run against a live ZoomInfo account.

Credits (ZoomInfo documentation): search does not consume credits; each enrich
record returned charges one unless already under management; "no match" does
not charge. ``credit_cost`` records the upper bound: records returned.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from nexbase.config import Settings, get_settings
from nexbase.enrichment.base import (
    EnrichedContact,
    EnrichmentCallResult,
    EnrichmentData,
    EnrichmentProvider,
)
from nexbase.logging_setup import get_logger

COMPANY_OUTPUT_FIELDS = [
    "id", "name", "website", "domainList", "employeeCount", "primaryIndustry",
    "industries", "city", "state", "country",
]
CONTACT_OUTPUT_FIELDS = [
    "id", "firstName", "lastName", "email", "jobTitle", "contactAccuracyScore",
    "externalUrls", "companyId", "companyName", "companyWebsite", "managementLevel",
]

#: ZoomInfo tokens are valid for 60 minutes; refresh after 55 as documented.
_TOKEN_TTL_SECONDS = 55 * 60
#: Documented maximum inputs per enrich request.
MAX_ENRICH_INPUTS = 25


@dataclass
class ApiCall:
    """One request that left the process, for enrichment_logs."""

    endpoint: str
    payload: dict
    status: str
    records: int = 0
    error: str | None = None
    #: Upper bound: every record returned by an enrich endpoint may charge one.
    credit_cost: float = 0.0
    billable: bool = False


@dataclass
class ZoomInfoCompany:
    id: Any
    name: str | None
    website: str | None
    domains: list[str]
    employee_count: int | None
    primary_industry: str | None
    city: str | None
    state: str | None
    country: str | None
    raw: dict = field(default_factory=dict)


class ZoomInfoProvider(EnrichmentProvider):
    name = "ZOOMINFO"

    def __init__(self, settings: Settings | None = None, logger=None, transport=None) -> None:
        self.settings = settings or get_settings()
        self.log = logger or get_logger("nexbase.enrichment.zoominfo")
        #: httpx transport override, so tests run against recorded fixtures.
        self._transport = transport
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._lock = threading.Lock()
        self.calls: list[ApiCall] = []

    def is_configured(self) -> bool:
        return bool(self.settings.zoominfo_api_key and self.settings.zoominfo_api_url)

    def _url(self, path: str) -> str:
        return f"{self.settings.zoominfo_api_url.rstrip('/')}/{path.lstrip('/')}"

    def _client(self) -> httpx.Client:
        return httpx.Client(transport=self._transport,
                            timeout=self.settings.request_timeout_seconds)

    # ------------------------------------------------------------------
    def _get_token(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token
            key = self.settings.zoominfo_api_key
            if ":" in key:
                username, password = key.split(":", 1)
                with self._client() as client:
                    response = client.post(self._url("authenticate"),
                                           json={"username": username, "password": password})
                response.raise_for_status()
                token = response.json().get("jwt")
                if not token:
                    raise RuntimeError("ZoomInfo authenticate returned no jwt")
            else:
                token = key  # a pre-issued JWT
            self._token = token
            self._token_expires_at = time.monotonic() + _TOKEN_TTL_SECONDS
            return token

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        reraise=True,
    )
    def _post(self, path: str, payload: dict) -> dict[str, Any]:
        with self._client() as client:
            response = client.post(
                self._url(path), json=payload,
                headers={"Authorization": f"Bearer {self._get_token()}",
                         "Content-Type": "application/json"},
            )
        response.raise_for_status()
        return response.json()

    def _call(self, path: str, payload: dict) -> tuple[dict | None, ApiCall]:
        call = ApiCall(endpoint=self._url(path), payload=payload, status="SUCCESS")
        try:
            body = self._post(path, payload)
        except Exception as exc:
            call.status, call.error = "ERROR", str(exc)
            # An error returns no record, so ZoomInfo charges no credit.
            call.billable = False
            self.calls.append(call)
            self.log.warning("zoominfo_call_failed", endpoint=path, error=str(exc))
            return None, call
        self.calls.append(call)
        call.billable = True
        return body, call

    # ------------------------------------------------------------------
    def match_companies(self, company_name: str | None,
                        domain: str | None) -> list[ZoomInfoCompany]:
        """Company Enrich candidates for a website and/or name. Empty on no match."""
        match_input: dict[str, Any] = {}
        if domain:
            match_input["companyWebsite"] = f"http://www.{domain.removeprefix('www.')}"
        if company_name:
            match_input["companyName"] = company_name
        if not match_input:
            return []
        body, call = self._call("enrich/company", {
            "matchCompanyInput": [match_input], "outputFields": COMPANY_OUTPUT_FIELDS,
        })
        rows = _result_rows(body)
        call.records = len(rows)
        call.credit_cost = float(len(rows))
        if body is not None and not rows:
            call.status = "NO_MATCH"
        return [_company(row) for row in rows]

    def search_contacts(self, company_id: Any, job_titles: list[str]) -> list[dict]:
        """Contact Search previews (no emails) at one ZoomInfo company."""
        body, call = self._call("search/contact", {
            "companyId": str(company_id), "jobTitle": " OR ".join(job_titles),
            "rpp": 25, "page": 1,
        })
        rows = [r for r in (body or {}).get("data") or [] if isinstance(r, dict)]
        call.records = len(rows)
        return rows

    def enrich_contacts(self, person_ids: list[Any]) -> list[EnrichedContact]:
        """Contact Enrich full records for ZoomInfo person ids (at most 25)."""
        ids = list(person_ids)[:MAX_ENRICH_INPUTS]
        if not ids:
            return []
        body, call = self._call("enrich/contact", {
            "matchPersonInput": [{"personId": pid} for pid in ids],
            "outputFields": CONTACT_OUTPUT_FIELDS,
        })
        rows = _result_rows(body)
        call.records = len(rows)
        call.credit_cost = float(len(rows))
        if body is not None and not rows:
            call.status = "NO_MATCH"
        return [_contact(row) for row in rows]

    # ------------------------------------------------------------------
    def enrich(self, company_name: str, domain: str | None,
               contacts: list[dict]) -> EnrichmentCallResult:
        """Provider interface: the company match only. The enrichment stage
        (``zoominfo_stage``) drives contact search and enrich with the Phase 6
        contacts in hand."""
        if not self.is_configured():
            return EnrichmentCallResult(provider=self.name, ok=False,
                                        error="NOT_CONFIGURED", billable=False)
        companies = self.match_companies(company_name, domain)
        call = self.calls[-1] if self.calls else None
        if not companies:
            return EnrichmentCallResult(
                provider=self.name, ok=False, error=getattr(call, "error", None) or "NO_MATCH",
                billable=bool(call and call.billable), credit_cost=0.0,
                endpoint=getattr(call, "endpoint", None))
        best = companies[0]
        return EnrichmentCallResult(
            provider=self.name, ok=True, billable=True,
            credit_cost=getattr(call, "credit_cost", 0.0), endpoint=call.endpoint,
            data=EnrichmentData(
                industry=best.primary_industry, employee_count=best.employee_count,
                employee_size_min=best.employee_count, employee_size_max=best.employee_count,
                location=", ".join(p for p in (best.city, best.state, best.country) if p) or None,
                domain=best.website, raw=best.raw))


def _result_rows(body: dict | None) -> list[dict]:
    """``data.result[].data[]`` flattened (documented enrich response shape)."""
    rows: list[dict] = []
    for entry in ((body or {}).get("data") or {}).get("result") or []:
        data = entry.get("data") if isinstance(entry, dict) else None
        if isinstance(data, dict):
            data = [data]
        rows.extend(r for r in data or [] if isinstance(r, dict))
    return rows


def _int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _company(row: dict) -> ZoomInfoCompany:
    domains = [d for d in (row.get("domainList") or []) if isinstance(d, str)]
    return ZoomInfoCompany(
        id=row.get("id"), name=row.get("name"), website=row.get("website"),
        domains=domains, employee_count=_int(row.get("employeeCount")),
        primary_industry=row.get("primaryIndustry"), city=row.get("city"),
        state=row.get("state"), country=row.get("country"),
        raw={k: row.get(k) for k in COMPANY_OUTPUT_FIELDS},
    )


def _contact(row: dict) -> EnrichedContact:
    company = row.get("company") if isinstance(row.get("company"), dict) else {}
    linkedin = next((u.get("url") for u in row.get("externalUrls") or []
                     if isinstance(u, dict) and "linkedin" in str(u.get("type", "")).lower()),
                    None)
    name = " ".join(p for p in (row.get("firstName"), row.get("lastName")) if p) or None
    return EnrichedContact(
        name=name, title=row.get("jobTitle"), email=row.get("email"), profile_url=linkedin,
        raw={
            "zoominfo_person_id": row.get("id"),
            "zoominfo_company_id": row.get("companyId") or company.get("id"),
            "company_name": row.get("companyName") or company.get("name"),
            "company_website": row.get("companyWebsite") or company.get("website"),
            "contact_accuracy_score": row.get("contactAccuracyScore"),
            "management_level": row.get("managementLevel"),
        },
    )
