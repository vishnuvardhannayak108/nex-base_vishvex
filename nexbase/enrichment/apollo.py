"""Apollo: FALLBACK provider of the enrichment waterfall (paid).

Every request and response field used here is taken from Apollo's published API
reference (docs.apollo.io, OpenAPI definitions, read 2026-09-17). Authentication
is the ``x-api-key`` header; base URL ``https://api.apollo.io/api/v1``.

* ``GET /organizations/enrich?domain=`` - 1 credit per organization; returns
  ``organization{id, name, primary_domain, website_url, estimated_num_employees,
  industry, ...}``.
* ``POST /mixed_people/api_search`` with query parameters ``person_titles[]``,
  ``include_similar_titles``, ``q_organization_domains_list[]``, ``per_page``,
  ``page`` - 0 credits; returns ``people[]`` with ``id``, ``first_name``,
  ``last_name_obfuscated``, ``title``, ``has_email`` and ``organization{name}``,
  and never an email. The domain filter also matches *previous* employers.
* ``POST /people/match`` with query parameters ``id``, or ``name`` /
  ``first_name`` + ``last_name`` with ``domain`` - returns ``person{id, name,
  title, email, email_status, linkedin_url, organization_id,
  organization{id, primary_domain, website_url}}``. Credits are charged only when
  data is found (1 for demographics or email). ``reveal_personal_emails`` is left
  at its default (false): personal free-mail addresses are not requested.

Documented errors: 401 (bad key), 403 (key not scoped for the endpoint), 422,
429 (rate limit). Nothing here has been run against a live Apollo account.
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
from nexbase.contacts.extraction import infer_priority
from nexbase.enrichment.base import (
    FAILURE_STATUSES,
    PROVIDER_NO_MATCH,
    PROVIDER_RATE_LIMITED,
    PROVIDER_SUCCESS,
    PROVIDER_UNAVAILABLE,
    CompanyContext,
    EnrichmentCallResult,
    EnrichmentData,
    EnrichmentNeed,
    EnrichmentProvider,
    FallbackProvider,
    ProviderCall,
    ProviderContact,
    ProviderOutcome,
    failure_status,
)
from nexbase.enrichment.waterfall import (
    POC_SEARCH_TITLES,
    domain_root,
    has_personal_email,
    person_key,
)
from nexbase.logging_setup import get_logger


class ApolloProvider(EnrichmentProvider):
    name = "APOLLO"

    def __init__(self, settings: Settings | None = None, logger=None, transport=None) -> None:
        self.settings = settings or get_settings()
        self.log = logger or get_logger("nexbase.enrichment.apollo")
        #: httpx transport override, so tests run against recorded fixtures.
        self._transport = transport
        self.calls: list[ProviderCall] = []

    def is_configured(self) -> bool:
        return bool(self.settings.apollo_api_key and self.settings.apollo_api_url)

    def _url(self, path: str) -> str:
        return f"{self.settings.apollo_api_url.rstrip('/')}/{path.lstrip('/')}"

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        reraise=True,
    )
    def _request(self, method: str, path: str, params: dict) -> dict[str, Any]:
        with httpx.Client(transport=self._transport,
                          timeout=self.settings.request_timeout_seconds) as client:
            response = client.request(method, self._url(path), params=params, headers={
                "x-api-key": self.settings.apollo_api_key, "accept": "application/json"})
        response.raise_for_status()
        return response.json()

    def _call(self, method: str, path: str, params: dict) -> tuple[dict | None, ProviderCall]:
        call = ProviderCall(endpoint=self._url(path), payload=params, status="SUCCESS")
        self.calls.append(call)
        try:
            body = self._request(method, path, params)
        except Exception as exc:
            call.status, call.error = failure_status(exc), str(exc)
            self.log.warning("apollo_call_failed", endpoint=path, status=call.status)
            return None, call
        call.billable = True
        return body, call

    # ------------------------------------------------------------------
    def search_people(self, domain: str, titles: list[str], per_page: int = 25) -> list[dict]:
        """People API Search previews (no emails, 0 credits) at a company domain."""
        body, call = self._call("POST", "mixed_people/api_search", {
            "person_titles[]": titles, "include_similar_titles": "false",
            "q_organization_domains_list[]": [domain], "per_page": per_page, "page": 1,
        })
        people = [p for p in (body or {}).get("people") or [] if isinstance(p, dict)]
        call.records = len(people)
        return people

    def match_person(self, **params) -> dict | None:
        """People Enrichment for one person; None when Apollo has no match."""
        body, call = self._call("POST", "people/match",
                                {k: v for k, v in params.items() if v})
        person = (body or {}).get("person")
        if not isinstance(person, dict):
            if body is not None:
                call.status = "NO_MATCH"
            return None
        call.records = 1
        found = person.get("email") or person.get("match_confidence") not in (None, "none")
        call.credit_cost = 1.0 if found else 0.0
        return person

    def enrich(self, company_name: str, domain: str | None,
               contacts: list[dict]) -> EnrichmentCallResult:
        """Company-level lookup (Organization Enrichment, 1 credit)."""
        if not self.is_configured():
            return EnrichmentCallResult(provider=self.name, ok=False,
                                        error="NOT_CONFIGURED", billable=False)
        if not domain:
            return EnrichmentCallResult(provider=self.name, ok=False,
                                        error="NO_DOMAIN", billable=False)
        body, call = self._call("GET", "organizations/enrich", {"domain": domain_root(domain)})
        org = (body or {}).get("organization")
        if not isinstance(org, dict):
            return EnrichmentCallResult(provider=self.name, ok=False,
                                        error=call.error or "NO_MATCH",
                                        billable=call.billable, endpoint=call.endpoint)
        call.credit_cost = 1.0
        count = org.get("estimated_num_employees")
        return EnrichmentCallResult(
            provider=self.name, ok=True, billable=True, credit_cost=1.0, endpoint=call.endpoint,
            data=EnrichmentData(
                industry=org.get("industry"), employee_count=count,
                employee_size_min=count, employee_size_max=count,
                location=", ".join(p for p in (org.get("city"), org.get("state"),
                                               org.get("country")) if p) or None,
                domain=org.get("primary_domain"),
                raw={k: org.get(k) for k in ("id", "name", "primary_domain", "website_url",
                                             "industry", "estimated_num_employees")}))


class ApolloAdapter(FallbackProvider):
    name = "APOLLO"

    def __init__(self, settings: Settings | None = None,
                 api: ApolloProvider | None = None, logger=None) -> None:
        self.settings = settings or get_settings()
        self.api = api or ApolloProvider(self.settings, logger)
        self.refresh_days = self.settings.apollo_refresh_days
        self.max_companies_per_run = self.settings.apollo_max_companies_per_run
        self.max_person_lookups_per_company = self.settings.apollo_max_person_lookups_per_company

    def is_configured(self) -> bool:
        return self.api.is_configured()

    def find(self, company: CompanyContext, need: EnrichmentNeed,
             existing: list[dict]) -> ProviderOutcome:
        start = len(self.api.calls)
        outcome = self._find(company, need, existing)
        outcome.calls = self.api.calls[start:]
        return outcome

    def _find(self, company, need, existing) -> ProviderOutcome:
        if not self.is_configured():
            return ProviderOutcome(self.name, PROVIDER_UNAVAILABLE, "NOT_CONFIGURED")
        if not company.domain:
            # Apollo's people filters cannot pin a company by name, and its domain
            # filter needs a domain: without one identity cannot be established.
            return ProviderOutcome(self.name, PROVIDER_NO_MATCH, "NO_DOMAIN_FOR_IDENTITY")
        root = domain_root(company.domain)
        outcome = ProviderOutcome(self.name, PROVIDER_SUCCESS,
                                  match_method="PERSON_ORGANIZATION_DOMAIN")

        # The people the previous provider could not complete come first.
        lookups: list[tuple[dict, str | None]] = [
            ({"name": p.get("name"), "domain": root}, p.get("name"))
            for p in need.people if p.get("name")]
        if need.full:
            start = len(self.api.calls)
            previews = self.api.search_people(root, POC_SEARCH_TITLES)
            failed = next((c for c in self.api.calls[start:] if c.status in FAILURE_STATUSES), None)
            if failed:
                outcome.status, outcome.reason = failed.status, failed.error
                return outcome
            strong = [c for c in existing if has_personal_email(c, company.domain)]
            previews = [p for p in previews
                        if p.get("has_email") and infer_priority(p.get("title")) is not None
                        and not any(_obfuscated_same(p, c.get("name")) for c in strong)]
            previews.sort(key=lambda p: infer_priority(p.get("title")))
            lookups += [({"id": p["id"]}, None) for p in previews]

        dropped = 0
        seen_ids: set = set()
        for params, expected_name in lookups[: need.limit]:
            person = self.api.match_person(**params)
            last = self.api.calls[-1]
            if last.status in FAILURE_STATUSES:
                outcome.status, outcome.reason = last.status, last.error
                if last.status in (PROVIDER_RATE_LIMITED, PROVIDER_UNAVAILABLE):
                    break  # do not keep calling a provider that refused
                continue
            if person is None or person.get("id") in seen_ids:
                continue
            seen_ids.add(person.get("id"))
            org = person.get("organization") or {}
            if domain_root(org.get("primary_domain") or org.get("website_url")) != root:
                dropped += 1  # works elsewhere, or Apollo cannot say where
                continue
            if expected_name and person_key(person.get("name")) != person_key(expected_name):
                dropped += 1  # a different person than the one asked about
                continue
            outcome.provider_company_id = person.get("organization_id") or org.get("id")
            outcome.contacts.append(ProviderContact(
                name=person.get("name"), title=person.get("title"), email=person.get("email"),
                profile_url=person.get("linkedin_url"), person_id=person.get("id"),
                extraction_method="apollo_people_match", evidence_url=self.api._url("people/match"),
                raw={"email_status": person.get("email_status"),
                     "match_confidence": person.get("match_confidence"),
                     "organization_id": outcome.provider_company_id}))
        if not outcome.contacts and outcome.status == PROVIDER_SUCCESS:
            outcome.status = PROVIDER_NO_MATCH
            outcome.reason = "IDENTITY_UNRESOLVED" if dropped else "NO_PERSON_MATCH"
        return outcome


def _obfuscated_same(preview: dict, name: str | None) -> bool:
    """Search previews carry ``last_name_obfuscated`` ("Hu***n"): compare what is visible."""
    key = person_key(name)
    first = (preview.get("first_name") or "").lower()
    obfuscated = (preview.get("last_name_obfuscated") or "").lower()
    if not key or not first or "*" not in obfuscated:
        return False
    head, tail = obfuscated.split("*", 1)[0], obfuscated.rsplit("*", 1)[-1]
    return key[0] == first and key[1].startswith(head) and key[1].endswith(tail)
