"""ZoomInfo as the PRIMARY provider of the enrichment waterfall.

1. Match the company. With a known domain, only a ZoomInfo company whose website
   or domain list is on that domain matches. Without one, only a single
   candidate with the same normalized name in the same state matches. Anything
   else is PROVIDER_NO_MATCH (AMBIGUOUS / DOMAIN_MISMATCH / NO_MATCH), and
   nothing is attached.
2. Contact Search at that company for the POC titles (free; no emails).
3. Contact Enrich (one credit per record) only for POC candidates whose email
   would fill a gap - missing, or weaker than PERSONAL - strongest first, capped
   per company. When asked only for specific people, only they are looked up.
"""
from __future__ import annotations

from nexbase.config import Settings, get_settings
from nexbase.contacts.extraction import infer_priority
from nexbase.core.geo import us_state_of
from nexbase.enrichment.base import (
    FAILURE_STATUSES,
    PROVIDER_NO_MATCH,
    PROVIDER_SUCCESS,
    PROVIDER_UNAVAILABLE,
    CompanyContext,
    EnrichmentNeed,
    FallbackProvider,
    ProviderContact,
    ProviderOutcome,
)
from nexbase.enrichment.waterfall import (
    POC_SEARCH_TITLES,
    domain_root,
    has_personal_email,
    person_key,
)
from nexbase.enrichment.zoominfo import ZoomInfoCompany, ZoomInfoProvider
from nexbase.pipeline.normalize import normalize_company_name


def match_company(candidates: list[ZoomInfoCompany],
                  company: CompanyContext) -> tuple[ZoomInfoCompany | None, str | None, str]:
    """``(company, match method, reason)``: the one ZoomInfo company this is, or why not."""
    if not candidates:
        return None, None, "NO_MATCH"
    if company.domain:
        root = domain_root(company.domain)
        on_domain = {c.id: c for c in candidates
                     if root in {domain_root(d) for d in [c.website or "", *c.domains]}}
        if len(on_domain) == 1:
            return next(iter(on_domain.values())), "DOMAIN", "MATCHED"
        return None, None, "AMBIGUOUS" if on_domain else "DOMAIN_MISMATCH"
    same = {c.id: c for c in candidates
            if normalize_company_name(c.name) == company.name_normalized
            and (not company.states or us_state_of(c.state or "") in company.states)}
    if len(same) == 1 and company.states:
        return next(iter(same.values())), "NAME_AND_STATE", "MATCHED"
    return None, None, "AMBIGUOUS" if same or len(candidates) > 1 else "NO_MATCH"


class ZoomInfoAdapter(FallbackProvider):
    name = "ZOOMINFO"

    def __init__(self, settings: Settings | None = None,
                 api: ZoomInfoProvider | None = None, logger=None) -> None:
        self.settings = settings or get_settings()
        self.api = api or ZoomInfoProvider(self.settings, logger)
        self.refresh_days = self.settings.zoominfo_refresh_days
        self.max_companies_per_run = self.settings.zoominfo_max_companies_per_run
        self.max_person_lookups_per_company = self.settings.zoominfo_max_contact_enrich_per_company

    def is_configured(self) -> bool:
        return self.api.is_configured()

    def find(self, company: CompanyContext, need: EnrichmentNeed,
             existing: list[dict]) -> ProviderOutcome:
        start = len(self.api.calls)
        outcome = self._find(company, need, existing)
        outcome.calls = self.api.calls[start:]
        return outcome

    def _failed(self, start: int):
        return next((c for c in self.api.calls[start:] if c.status in FAILURE_STATUSES), None)

    def _find(self, company, need, existing) -> ProviderOutcome:
        if not self.is_configured():
            return ProviderOutcome(self.name, PROVIDER_UNAVAILABLE, "NOT_CONFIGURED")
        start = len(self.api.calls)
        candidates = self.api.match_companies(company.name, company.domain)
        if failed := self._failed(start):
            return ProviderOutcome(self.name, failed.status, failed.error)
        match, method, reason = match_company(candidates, company)
        if match is None:
            return ProviderOutcome(self.name, PROVIDER_NO_MATCH, reason)
        outcome = ProviderOutcome(self.name, PROVIDER_SUCCESS, match_method=method,
                                  provider_company_id=match.id)

        previews = [
            row for row in self.api.search_contacts(match.id, POC_SEARCH_TITLES)
            if infer_priority(row.get("jobTitle")) is not None
            and _same_company((row.get("company") or {}).get("id"), match.id)
        ]
        if failed := self._failed(start):
            outcome.status, outcome.reason = failed.status, failed.error
            return outcome
        if not need.full:
            wanted = {person_key(p.get("name")) for p in need.people}
            previews = [row for row in previews if person_key(_full_name(row)) in wanted]

        to_enrich = self._worth_enriching(previews, existing, company.domain, need.limit)
        enriched = self.api.enrich_contacts([row["id"] for row in to_enrich])
        if failed := self._failed(start):
            outcome.status, outcome.reason = failed.status, failed.error
        enriched_ids = set()
        for person in enriched:
            pid = person.raw.get("zoominfo_person_id")
            enriched_ids.add(pid)
            if not _same_company(person.raw.get("zoominfo_company_id"), match.id):
                continue  # ZoomInfo said this person works elsewhere
            score = person.raw.get("contact_accuracy_score")
            outcome.contacts.append(ProviderContact(
                name=person.name, title=person.title, email=person.email,
                profile_url=person.profile_url, person_id=pid,
                extraction_method="zoominfo_enrich_contact",
                evidence_url=self.api._url("enrich/contact"),
                confidence=score / 100 if isinstance(score, (int, float)) else None,
                raw=person.raw))
        if need.full:
            # A preview not enriched still names a POC at the company, for free.
            for row in previews:
                if row.get("id") in enriched_ids:
                    continue
                score = row.get("contactAccuracyScore")
                outcome.contacts.append(ProviderContact(
                    name=_full_name(row), title=row.get("jobTitle"), person_id=row.get("id"),
                    extraction_method="zoominfo_search_contact",
                    evidence_url=self.api._url("search/contact"),
                    confidence=score / 100 if isinstance(score, (int, float)) else None,
                    raw={"has_email": row.get("hasEmail")}))
        if not outcome.contacts and outcome.status == PROVIDER_SUCCESS:
            outcome.status, outcome.reason = PROVIDER_NO_MATCH, "NO_POC_FOUND"
        return outcome

    @staticmethod
    def _worth_enriching(previews: list[dict], existing: list[dict], domain,
                         limit: int) -> list[dict]:
        """Previews whose enrichment would fill a gap, strongest POC first."""
        strong = {person_key(c.get("name")) for c in existing if has_personal_email(c, domain)}
        wanted = [row for row in previews
                  if row.get("hasEmail") and person_key(_full_name(row)) not in strong]
        wanted.sort(key=lambda r: (infer_priority(r.get("jobTitle")),
                                   -(r.get("contactAccuracyScore") or 0)))
        return wanted[:limit]


def _full_name(row: dict) -> str | None:
    return " ".join(p for p in (row.get("firstName"), row.get("lastName")) if p) or None


def _same_company(value, company_id) -> bool:
    """True unless ZoomInfo named a different company id."""
    return value is None or str(value) == str(company_id)
