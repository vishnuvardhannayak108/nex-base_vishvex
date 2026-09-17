"""Enrichment waterfall: ZoomInfo PRIMARY, Apollo and Apify FALLBACK only.

Runs per QUALIFIED company after free / public contact discovery and email
classification, and before email verification.

Primary (ZoomInfo) is consulted when the company has fewer than
``CONTACTS_TARGET`` named POC contacts with a PERSONAL company-domain email.

A fallback provider is consulted only when the previous provider could not
provide what was required - never because it might return more:

* previous provider failed (PROVIDER_NO_MATCH, _ERROR, _TIMEOUT, _RATE_LIMITED,
  _UNAVAILABLE) and the company is still below that target: the fallback
  repeats the whole task;
* previous provider was PROVIDER_PARTIAL - it resolved the company but a POC it
  supplied or was asked about still lacks a PERSONAL company-domain email: the
  fallback is asked for those people's emails only;
* previous provider was PROVIDER_SUCCESS, or nothing was called because nothing
  was missing: no further provider is called.

Quality always beats provider order: an email replaces another only when its
class is strictly stronger (PERSONAL > ROLE > DOMAIN_MISMATCH >
EXTERNAL_UNVERIFIED > PORTAL_GENERATED), so on a tie the earlier source (public
evidence, then ZoomInfo, then Apollo, then Apify) keeps it. Replaced emails and
every provider-supplied field keep their provenance.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from nexbase.config import Settings, get_settings
from nexbase.contacts.extraction import infer_priority
from nexbase.email.discovery import EMAIL_CLASS_STRENGTH, PERSONAL, classify_email
from nexbase.enrichment.base import (
    FAILURE_STATUSES,
    PROVIDER_ERROR,
    PROVIDER_PARTIAL,
    PROVIDER_RATE_LIMITED,
    PROVIDER_SUCCESS,
    PROVIDER_UNAVAILABLE,
    CompanyContext,
    EnrichmentNeed,
    FallbackProvider,
    ProviderOutcome,
    utcnow,
)
from nexbase.logging_setup import get_logger
from nexbase.pipeline.normalize import extract_host, registrable_domain

#: The Master Plan's POC titles, as provider job-title search terms.
POC_SEARCH_TITLES = [
    "Owner", "CEO", "President", "Managing Partner",
    "COO", "VP Operations", "Director of Operations", "General Manager",
    "HR Director", "HR Manager", "Head of HR", "Head of People", "Talent Acquisition",
    "Plant Manager", "Operations Manager",
]

PUBLIC = "PUBLIC"
#: Evidence order when two sources are equally strong.
SOURCE_ORDER = (PUBLIC, "ZOOMINFO", "APOLLO", "APIFY")
NOT_CALLED = "NOT_CALLED"
_NAME_NOISE = frozenset({"jr", "sr", "ii", "iii", "iv", "mr", "mrs", "ms", "dr"})


def person_key(name: str | None) -> tuple[str, str] | None:
    """(first, last) name tokens; middle initials and suffixes ignored."""
    tokens = [t for t in re.findall(r"[a-z]+", (name or "").lower())
              if len(t) > 1 and t not in _NAME_NOISE]
    return (tokens[0], tokens[-1]) if len(tokens) >= 2 else None


def domain_root(url_or_host: str | None) -> str:
    return registrable_domain(extract_host(url_or_host or ""))


def email_strength(email: str | None, domain: str | None) -> int:
    return EMAIL_CLASS_STRENGTH[classify_email(email, domain)] if email else -1


def has_personal_email(contact: dict, domain: str | None) -> bool:
    return bool(contact.get("email")) and classify_email(contact["email"], domain) == PERSONAL


def _origins(contact: dict) -> list[str]:
    return [o for o in (contact.get("origin") or PUBLIC).split("+") if o]


def _source_rank(contact: dict) -> int:
    first = _origins(contact)[0]
    return SOURCE_ORDER.index(first) if first in SOURCE_ORDER else len(SOURCE_ORDER)


@dataclass
class CompanyResult:
    attempts: list[dict] = field(default_factory=list)
    #: Provider-supplied emails, for the email classification pass.
    provider_emails: list[dict] = field(default_factory=list)
    credits: float = 0.0

    @property
    def providers_called(self) -> list[str]:
        return [a["provider"] for a in self.attempts if a["called"]]

    def as_dict(self) -> dict:
        return {"attempts": self.attempts, "providers_called": self.providers_called,
                "credits": self.credits}


class EnrichmentWaterfall:
    def __init__(self, providers: list[FallbackProvider], repo,
                 settings: Settings | None = None, logger=None) -> None:
        #: In priority order: primary first.
        self.providers = providers
        self.repo = repo
        self.settings = settings or get_settings()
        self.log = logger or get_logger("nexbase.enrichment.waterfall")
        self._companies: dict[str, int] = {}
        #: provider -> status that disabled it for the rest of the run.
        self._disabled: dict[str, str] = {}

    # ------------------------------------------------------------------
    def enrich_company(self, company: CompanyContext, lead, company_id) -> CompanyResult:
        """Run the waterfall for one QUALIFIED company; ``lead.contacts`` is updated."""
        result = CompanyResult()
        previous: dict | None = None
        for index, provider in enumerate(self.providers):
            need, reason = self._decide(index, previous, lead.contacts, company.domain,
                                        provider)
            if need is None:
                result.attempts.append({"provider": provider.name, "called": False,
                                        "status": NOT_CALLED, "reason": reason, "at": utcnow()})
                continue
            attempt = self._attempt(provider, company, need, lead, company_id, result)
            attempt["fallback_reason"] = reason if index else None
            result.attempts.append(attempt)
            previous = attempt
        self._persist(company_id, lead)
        return result

    # ------------------------------------------------------------------
    def _decide(self, index: int, previous: dict | None, contacts: list[dict],
                domain: str | None, provider: FallbackProvider):
        """``(need, reason)``: what to ask ``provider`` for, or None and why not."""
        target = self.settings.contacts_target
        missing_email = self._missing_email(contacts, domain)
        below_target = sum(1 for c in contacts
                           if c.get("title_priority") and has_personal_email(c, domain)) < target
        limit = provider.max_person_lookups_per_company
        if index == 0:
            if not below_target:
                return None, "NOTHING_MISSING"
            return EnrichmentNeed(full=True, people=missing_email, limit=limit), "PRIMARY"
        if previous is None:
            return None, "NOTHING_MISSING"
        status = previous["status"]
        if status == PROVIDER_SUCCESS:
            return None, f"{previous['provider']} {PROVIDER_SUCCESS}"
        if status == PROVIDER_PARTIAL:
            if not missing_email:
                return None, "NOTHING_MISSING"
            names = ", ".join(c.get("name") or "?" for c in missing_email)
            return (EnrichmentNeed(full=False, people=missing_email, limit=limit),
                    f"{previous['provider']} {PROVIDER_PARTIAL}: no PERSONAL email for {names}")
        if status in FAILURE_STATUSES:
            if not below_target:
                return None, "NOTHING_MISSING"
            detail = f": {previous['reason']}" if previous.get("reason") else ""
            return (EnrichmentNeed(full=True, people=missing_email, limit=limit),
                    f"{previous['provider']} {status}{detail}")
        return None, "NOTHING_MISSING"

    def _missing_email(self, contacts: list[dict], domain: str | None) -> list[dict]:
        """Top POC contacts (up to the target) without a PERSONAL company-domain email."""
        pocs = [c for c in contacts if c.get("title_priority")]
        return [c for c in pocs[: self.settings.contacts_target]
                if not has_personal_email(c, domain)]

    # ------------------------------------------------------------------
    def _attempt(self, provider: FallbackProvider, company: CompanyContext,
                 need: EnrichmentNeed, lead, company_id, result: CompanyResult) -> dict:
        outcome = self._consult(provider, company, need, lead.contacts, company_id)
        touched, added, upgraded = self._merge(lead, outcome, company.domain, result)

        if outcome.status == PROVIDER_SUCCESS:
            asked = {person_key(p.get("name")) for p in need.people}
            unfilled = [c for c in self._missing_email(lead.contacts, company.domain)
                        if person_key(c.get("name")) in asked or id(c) in touched]
            if unfilled:
                outcome.status = PROVIDER_PARTIAL
                outcome.reason = "EMAIL_MISSING"

        credits = sum(c.credit_cost for c in outcome.calls)
        result.credits += credits
        for call in outcome.calls:
            self.repo.insert_enrichment_log({
                "company_id": company_id, "provider": provider.name, "endpoint": call.endpoint,
                "payload": call.payload, "status": call.status, "billable": call.billable,
                "credit_cost": call.credit_cost,
                "result": {"records": call.records, "error": call.error, "at": call.at,
                           "attempt_status": outcome.status, "attempt_reason": outcome.reason,
                           "match_method": outcome.match_method},
            })
        if outcome.provider_company_id is not None:
            self.repo.insert_evidence({
                "record_type": "COMPANY", "record_id": company_id,
                "key": f"{provider.name.lower()}_company_match",
                "value": str(outcome.provider_company_id), "url": None,
                "source_type": provider.name, "source_priority": 4,
                "raw_payload": {"match_method": outcome.match_method, "at": utcnow()},
            })
        if any(c.status in (PROVIDER_RATE_LIMITED, PROVIDER_UNAVAILABLE) for c in outcome.calls):
            # Rate limited or credentials refused: not tried again this run.
            self._disabled[provider.name] = next(
                c.status for c in outcome.calls
                if c.status in (PROVIDER_RATE_LIMITED, PROVIDER_UNAVAILABLE))

        self.log.info("enrichment_attempt", provider=provider.name, status=outcome.status,
                      reason=outcome.reason, contacts_added=added, contacts_upgraded=upgraded,
                      credits=credits)
        return {
            "provider": provider.name, "called": bool(outcome.calls),
            "consulted": True,
            "status": outcome.status, "reason": outcome.reason,
            "match_method": outcome.match_method,
            "provider_company_id": outcome.provider_company_id,
            "need": {"full": need.full, "people": [p.get("name") for p in need.people]},
            "person_ids": [c.person_id for c in outcome.contacts if c.person_id is not None],
            "fields_supplied": sorted({f"{c.get('name')}:{f}" for c in lead.contacts
                                       for f, p in (c.get("field_provenance") or {}).items()
                                       if p.get("provider") == provider.name and p.get("new")}),
            "contacts_added": added, "contacts_upgraded": upgraded,
            "requests": len(outcome.calls),
            "billable": any(c.billable for c in outcome.calls), "credits": credits,
            "at": utcnow(),
        }

    def _consult(self, provider, company, need, contacts, company_id) -> ProviderOutcome:
        if provider.name in self._disabled:
            return ProviderOutcome(provider.name, PROVIDER_UNAVAILABLE,
                                   f"DISABLED_FOR_RUN_AFTER_{self._disabled[provider.name]}")
        if not provider.is_configured():
            return ProviderOutcome(provider.name, PROVIDER_UNAVAILABLE, "NOT_CONFIGURED")
        cached = self._cached(provider, company_id)
        if cached is not None:
            return cached
        used = self._companies.get(provider.name, 0)
        if used >= provider.max_companies_per_run:
            return ProviderOutcome(provider.name, PROVIDER_UNAVAILABLE, "COMPANY_BUDGET_EXHAUSTED")
        self._companies[provider.name] = used + 1
        try:
            return provider.find(company, need, contacts)
        except Exception as exc:  # a provider bug must not stop the run
            self.log.warning("enrichment_provider_crashed", provider=provider.name, error=str(exc))
            return ProviderOutcome(provider.name, PROVIDER_ERROR, str(exc))

    # ------------------------------------------------------------------
    def _merge(self, lead, outcome: ProviderOutcome, domain, result: CompanyResult):
        """Merge a provider's contacts: same person is one contact, quality decides."""
        provider = outcome.provider
        contacts = [c if isinstance(c, dict) else dict(c) for c in lead.contacts]
        touched: set[int] = set()
        added = upgraded = 0
        for found in outcome.contacts:
            source = {
                "provider": provider, "source": provider.lower(), "source_type": provider,
                "evidence_url": found.evidence_url, "extraction_method": found.extraction_method,
                "person_id": found.person_id, "match_method": outcome.match_method,
                "confidence": found.confidence, "enriched_at": utcnow(), "new": True,
            }
            match = next((c for c in contacts if _same_person(c, found, provider)), None)
            if match is None:
                if infer_priority(found.title) is None:
                    continue
                contact = {
                    "id": None, "name": found.name, "title": found.title,
                    "title_priority": infer_priority(found.title), "email": found.email,
                    "profile_url": found.profile_url, "rank_score": None,
                    "discovery_stage": "ENRICHMENT", "source_type": provider,
                    "source_priority": 4, "source": provider.lower(),
                    "source_url": found.evidence_url, "extraction": found.extraction_method,
                    "origin": provider, "provider_person_ids": {provider: found.person_id},
                    "field_provenance": {f: source for f, v in (
                        ("name", found.name), ("title", found.title), ("email", found.email),
                        ("profile_url", found.profile_url)) if v},
                }
                contacts.append(contact)
                touched.add(id(contact))
                added += 1
            else:
                match.setdefault("provider_person_ids", {})[provider] = found.person_id
                if provider not in _origins(match):
                    match["origin"] = "+".join([*_origins(match), provider])
                touched.add(id(match))
                if self._fill(match, found, source, domain):
                    upgraded += 1
            if found.email:
                result.provider_emails.append({
                    "email": found.email, "source": provider.lower(), "source_type": provider,
                    "evidence_url": found.evidence_url,
                    "extraction_method": found.extraction_method,
                    "discovery_stage": "ENRICHMENT", "contact_name": found.name,
                    "contact_title": found.title,
                })

        contacts.sort(key=lambda c: (
            c.get("title_priority") or 9,
            -email_strength(c.get("email"), domain),
            _source_rank(c),
            -(c.get("rank_score") or 0),
        ))
        lead.contacts = contacts[: self.settings.contacts_max]
        return touched, added, upgraded

    @staticmethod
    def _fill(contact: dict, found, source: dict, domain) -> bool:
        """Fill missing fields and upgrade a strictly weaker email; never downgrade."""
        provenance = contact.setdefault("field_provenance", {})
        changed = False
        if found.email and email_strength(found.email, domain) > email_strength(contact.get("email"), domain):
            if contact.get("email"):
                contact.setdefault("replaced_emails", []).append({
                    "email": contact["email"],
                    "email_class": classify_email(contact["email"], domain),
                    "provenance": provenance.get("email") or {
                        "source": contact.get("source"), "source_type": contact.get("source_type"),
                        "evidence_url": contact.get("source_url"),
                        "extraction_method": contact.get("extraction")},
                    "replaced_by": source["provider"], "replaced_at": source["enriched_at"],
                })
            contact["email"] = found.email
            provenance["email"] = source
            changed = True
        if found.profile_url and not contact.get("profile_url"):
            contact["profile_url"] = found.profile_url
            provenance["profile_url"] = source
            changed = True
        if changed:
            contact["changed"] = True
        return changed

    # ------------------------------------------------------------------
    def _cached(self, provider: FallbackProvider, company_id) -> ProviderOutcome | None:
        """Contacts this provider supplied within its refresh window, or None to call it."""
        if (provider.refresh_days is None or company_id is None
                or not getattr(self.repo, "configured", False)):
            return None
        logs = self.repo.select("enrichment_logs", "created_at, status",
                                filters={"company_id": company_id, "provider": provider.name},
                                order_by="created_at", limit=1)
        if not logs:
            return None
        created = _parse_time(logs[0].get("created_at"))
        if created is None or datetime.now(timezone.utc) - created > timedelta(
                days=provider.refresh_days):
            return None
        from nexbase.enrichment.base import ProviderContact

        rows = self.repo.select(
            "contacts", "id, name, title, title_priority, email, profile_url, raw_payload",
            filters={"company_id": company_id, "source_type": provider.name})
        return ProviderOutcome(provider.name, PROVIDER_SUCCESS, reason="CACHED", contacts=[
            ProviderContact(name=row.get("name"), title=row.get("title"), email=row.get("email"),
                            profile_url=row.get("profile_url"),
                            person_id=((row.get("raw_payload") or {}).get("provider_person_ids")
                                       or {}).get(provider.name),
                            extraction_method="cached", raw=row.get("raw_payload") or {})
            for row in rows])

    def _persist(self, company_id, lead) -> None:
        for contact in lead.contacts:
            new_provenance = {f: p for f, p in (contact.get("field_provenance") or {}).items()
                              if p.get("new")}
            if not new_provenance and not contact.get("changed"):
                continue
            row = {
                "company_id": company_id, "name": contact.get("name"),
                "title": contact.get("title"), "title_priority": contact.get("title_priority"),
                "email": contact.get("email"), "profile_url": contact.get("profile_url"),
                "discovery_stage": contact.get("discovery_stage"),
                "source_type": contact.get("source_type"),
                "source_priority": contact.get("source_priority"),
            }
            if contact.get("id"):
                # An existing contact keeps its row and provenance; only the
                # fields a provider filled change, each backed by an evidence row.
                if contact.get("changed"):
                    self.repo.update_contact(contact["id"], {
                        k: row[k] for k in ("email", "profile_url")})
            else:
                contact["id"] = self.repo.upsert_contact({**row, "raw_payload": {
                    k: contact.get(k) for k in ("origin", "provider_person_ids",
                                                "extraction", "source_url")}
                    | {"field_provenance": _public(contact.get("field_provenance"))}})
            for field_name, source in new_provenance.items():
                self.repo.insert_evidence({
                    "record_type": "CONTACT", "record_id": contact.get("id"),
                    "key": f"{source['provider'].lower()}_{field_name}",
                    "value": str(contact.get(field_name)), "url": source.get("evidence_url"),
                    "source_type": source["provider"], "source_priority": 4,
                    "raw_payload": {k: v for k, v in source.items() if k != "new"},
                })
                source.pop("new", None)
            contact.pop("changed", None)


def _public(provenance: dict | None) -> dict:
    return {f: {k: v for k, v in p.items() if k != "new"} for f, p in (provenance or {}).items()}


def _same_person(contact: dict, found, provider: str) -> bool:
    if contact.get("email") and found.email and contact["email"].lower() == found.email.lower():
        return True
    ids = contact.get("provider_person_ids") or {}
    if found.person_id is not None and ids.get(provider) == found.person_id:
        return True
    key = person_key(contact.get("name"))
    return key is not None and key == person_key(found.name)


def _parse_time(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
