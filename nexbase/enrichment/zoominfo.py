"""ZoomInfo enrichment provider (primary, paid).

Implements ZoomInfo's real Enterprise API shape:

* ``POST /authenticate`` exchanges username + private key (or a client-id/
  secret pair) for a JWT, cached for the token lifetime.
* ``POST /enrich/company-master`` with ``matchCompanyInput`` and an explicit
  ``outputFields`` list -> industry, employee count, location.
* ``POST /search/contact`` for decision-makers matching the brief's ladder.

Honest about configuration: with no credentials the provider reports
``NOT_CONFIGURED`` and ``billable=False`` rather than inventing a response or
logging a phantom paid call.
"""
from __future__ import annotations

import threading
import time
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

_COMPANY_OUTPUT_FIELDS = [
    "id", "name", "website", "primaryIndustry", "industries",
    "employeeCount", "revenue", "street", "city", "state", "country",
]

_CONTACT_MANAGEMENT_LEVELS = ["C Level Exec", "VP Level Exec", "Director", "Manager"]
_CONTACT_DEPARTMENTS = ["Operations", "Human Resources", "Executive"]

#: ZoomInfo tokens are valid for 60 minutes; refresh a little early.
_TOKEN_TTL_SECONDS = 55 * 60


class ZoomInfoProvider(EnrichmentProvider):
    name = "ZOOMINFO"

    def __init__(self, settings: Settings | None = None, logger=None) -> None:
        self.settings = settings or get_settings()
        self.log = logger or get_logger("nexbase.enrichment.zoominfo")
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._lock = threading.Lock()

    def is_configured(self) -> bool:
        return bool(self.settings.zoominfo_api_key and self.settings.zoominfo_api_url)

    def _url(self, path: str) -> str:
        return f"{self.settings.zoominfo_api_url.rstrip('/')}/{path.lstrip('/')}"

    # ------------------------------------------------------------------
    def _get_token(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token

            key = self.settings.zoominfo_api_key
            # Support both "username:private_key" and a pre-issued JWT.
            if ":" in key:
                username, secret = key.split(":", 1)
                payload = {"username": username, "password": secret}
                response = httpx.post(
                    self._url("authenticate"),
                    json=payload,
                    timeout=self.settings.request_timeout_seconds,
                )
                response.raise_for_status()
                token = response.json().get("jwt")
                if not token:
                    raise RuntimeError("ZoomInfo authenticate returned no jwt")
            else:
                token = key

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
        response = httpx.post(
            self._url(path),
            json=payload,
            headers={
                "Authorization": f"Bearer {self._get_token()}",
                "Content-Type": "application/json",
            },
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    # ------------------------------------------------------------------
    def enrich(
        self, company_name: str, domain: str | None, contacts: list[dict]
    ) -> EnrichmentCallResult:
        if not self.is_configured():
            return EnrichmentCallResult(
                provider=self.name, ok=False, error="NOT_CONFIGURED", billable=False
            )

        match_input: dict[str, Any] = {}
        if domain:
            match_input["companyWebsite"] = domain
        if company_name:
            match_input["companyName"] = company_name
        if not match_input:
            return EnrichmentCallResult(
                provider=self.name, ok=False, error="NO_MATCH_INPUT", billable=False
            )

        endpoint = self._url("enrich/company-master")
        try:
            payload = self._post(
                "enrich/company-master",
                {"matchCompanyInput": [match_input], "outputFields": _COMPANY_OUTPUT_FIELDS},
            )
        except Exception as exc:
            self.log.warning("zoominfo_enrich_failed", company=company_name, error=str(exc))
            return EnrichmentCallResult(
                provider=self.name,
                ok=False,
                error=str(exc),
                billable=True,
                credit_cost=1.0,
                endpoint=endpoint,
            )

        company = self._first_match(payload)
        if not company:
            return EnrichmentCallResult(
                provider=self.name,
                ok=False,
                error="NO_MATCH",
                billable=True,
                credit_cost=1.0,
                endpoint=endpoint,
            )

        data = self._parse_company(company)
        credits = 1.0

        people = self._search_contacts(company.get("id"), domain)
        data.contacts = people
        credits += len(people) * 1.0

        self.log.info(
            "zoominfo_enrich_success",
            company=company_name,
            employees=data.employee_count,
            contacts=len(people),
        )
        return EnrichmentCallResult(
            provider=self.name,
            ok=True,
            data=data,
            credit_cost=credits,
            billable=True,
            endpoint=endpoint,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _first_match(payload: dict[str, Any]) -> dict[str, Any] | None:
        results = (payload.get("data") or {}).get("result") or payload.get("result") or []
        for entry in results:
            matched = entry.get("data") if isinstance(entry, dict) else None
            if isinstance(matched, list) and matched:
                return matched[0]
            if isinstance(matched, dict):
                return matched
        return None

    def _search_contacts(self, company_id: Any, domain: str | None) -> list[EnrichedContact]:
        query: dict[str, Any] = {
            "managementLevel": ",".join(_CONTACT_MANAGEMENT_LEVELS),
            "department": ",".join(_CONTACT_DEPARTMENTS),
            "rpp": 10,
            "page": 1,
        }
        if company_id:
            query["companyId"] = str(company_id)
        elif domain:
            query["companyWebsite"] = domain
        else:
            return []

        try:
            payload = self._post("search/contact", query)
        except Exception as exc:
            self.log.warning("zoominfo_contact_search_failed", error=str(exc))
            return []

        rows = payload.get("data") or payload.get("contacts") or []
        out: list[EnrichedContact] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            out.append(
                EnrichedContact(
                    name=row.get("name")
                    or " ".join(p for p in (row.get("firstName"), row.get("lastName")) if p)
                    or None,
                    title=row.get("jobTitle") or row.get("title"),
                    email=row.get("email"),
                    profile_url=row.get("linkedInUrl") or row.get("contactAccuracyScore"),
                    raw={k: row.get(k) for k in ("id", "managementLevel", "jobFunction")},
                )
            )
        return out

    def _parse_company(self, company: dict[str, Any]) -> EnrichmentData:
        count = company.get("employeeCount")
        try:
            count = int(count) if count is not None else None
        except (TypeError, ValueError):
            count = None

        location = ", ".join(
            p for p in (company.get("city"), company.get("state"), company.get("country")) if p
        ) or None

        industries = company.get("industries")
        if isinstance(industries, list) and industries:
            industry = company.get("primaryIndustry") or industries[0]
        else:
            industry = company.get("primaryIndustry")
        if isinstance(industry, list):
            industry = industry[0] if industry else None

        return EnrichmentData(
            industry=industry,
            employee_size_min=count,
            employee_size_max=count,
            employee_count=count,
            location=location,
            domain=company.get("website"),
            raw={k: company.get(k) for k in _COMPANY_OUTPUT_FIELDS},
        )
