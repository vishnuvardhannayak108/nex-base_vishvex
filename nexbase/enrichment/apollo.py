"""Apollo enrichment provider (secondary source and cross-check, paid).

Implements Apollo's real API contract rather than a generic POST:

* ``POST /organizations/enrich`` keyed on ``domain`` (Apollo resolves on
  domain, not company name), returning ``estimated_num_employees`` and
  ``industry``.
* ``POST /mixed_people/search`` for decision-makers at that organization,
  filtered to the brief's P1-P4 seniorities.

Auth is the ``X-Api-Key`` header. Every call is reported with ``billable=True``
so credit accounting reflects actual spend; skipped calls report
``billable=False``.
"""
from __future__ import annotations

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

#: Apollo seniority values matching the brief's P1-P4 ladder.
_TARGET_SENIORITIES = ["owner", "founder", "c_suite", "partner", "vp", "head", "director", "manager"]
_TARGET_TITLES = [
    "Owner", "CEO", "President", "Managing Partner",
    "COO", "VP Operations", "Director of Operations", "General Manager",
    "HR Director", "HR Manager", "Head of People", "Talent Acquisition Manager",
    "Plant Manager", "Operations Manager",
]


class ApolloProvider(EnrichmentProvider):
    name = "APOLLO"

    def __init__(self, settings: Settings | None = None, logger=None) -> None:
        self.settings = settings or get_settings()
        self.log = logger or get_logger("nexbase.enrichment.apollo")

    def is_configured(self) -> bool:
        return bool(self.settings.apollo_api_key and self.settings.apollo_api_url)

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "X-Api-Key": self.settings.apollo_api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.settings.apollo_api_url.rstrip('/')}/{path.lstrip('/')}"

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
            headers=self._headers,
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
        if not domain:
            # Apollo resolves organizations on domain; a name-only lookup
            # burns a credit for an unreliable match.
            return EnrichmentCallResult(
                provider=self.name, ok=False, error="NO_DOMAIN", billable=False
            )

        endpoint = self._url("organizations/enrich")
        try:
            org_payload = self._post("organizations/enrich", {"domain": domain})
        except Exception as exc:
            self.log.warning("apollo_enrich_failed", company=company_name, error=str(exc))
            return EnrichmentCallResult(
                provider=self.name,
                ok=False,
                error=str(exc),
                billable=True,
                credit_cost=1.0,
                endpoint=endpoint,
            )

        org = org_payload.get("organization") or {}
        data = self._parse_org(org)
        credits = 1.0

        people = self._search_people(org.get("id"), domain)
        data.contacts = people
        credits += len(people) * 1.0

        self.log.info(
            "apollo_enrich_success",
            company=company_name,
            domain=domain,
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
    def _search_people(self, organization_id: str | None, domain: str) -> list[EnrichedContact]:
        payload: dict[str, Any] = {
            "person_seniorities": _TARGET_SENIORITIES,
            "person_titles": _TARGET_TITLES,
            "per_page": 10,
            "page": 1,
        }
        if organization_id:
            payload["organization_ids"] = [organization_id]
        else:
            payload["q_organization_domains"] = [domain]

        try:
            result = self._post("mixed_people/search", payload)
        except Exception as exc:
            self.log.warning("apollo_people_search_failed", domain=domain, error=str(exc))
            return []

        people = result.get("people") or result.get("contacts") or []
        out: list[EnrichedContact] = []
        for person in people:
            if not isinstance(person, dict):
                continue
            email = person.get("email")
            # Apollo returns placeholders when an email is locked.
            if isinstance(email, str) and "not_unlocked" in email:
                email = None
            out.append(
                EnrichedContact(
                    name=person.get("name")
                    or " ".join(
                        p for p in (person.get("first_name"), person.get("last_name")) if p
                    )
                    or None,
                    title=person.get("title"),
                    email=email,
                    profile_url=person.get("linkedin_url"),
                    raw={
                        k: person.get(k)
                        for k in ("id", "seniority", "departments", "city", "state")
                    },
                )
            )
        return out

    def _parse_org(self, org: dict[str, Any]) -> EnrichmentData:
        count = org.get("estimated_num_employees")
        try:
            count = int(count) if count is not None else None
        except (TypeError, ValueError):
            count = None

        location = ", ".join(
            p for p in (org.get("city"), org.get("state"), org.get("country")) if p
        ) or None

        return EnrichmentData(
            industry=org.get("industry"),
            employee_size_min=count,
            employee_size_max=count,
            employee_count=count,
            location=location,
            domain=org.get("primary_domain") or org.get("website_url"),
            raw={
                k: org.get(k)
                for k in (
                    "id", "name", "website_url", "primary_domain", "industry",
                    "estimated_num_employees", "annual_revenue", "keywords",
                )
            },
        )
