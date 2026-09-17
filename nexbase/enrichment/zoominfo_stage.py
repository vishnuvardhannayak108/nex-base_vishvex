"""ZoomInfo enrichment for QUALIFIED companies (Master Plan Phase 7).

Runs after free / public contact discovery and email classification, before
Apollo, Apify and email verification. Per company:

1. Skip without spending when nothing is missing (enough named P1-P4 contacts
   with a PERSONAL company-domain email already), when ZoomInfo was consulted
   within ``ZOOMINFO_REFRESH_DAYS`` (its stored contacts are reused), or when the
   run's company budget is spent.
2. Match the company. With a known domain, only a ZoomInfo company whose website
   or domain list is on that domain matches. Without one, only a single
   candidate with the same normalized name in the same state matches. Anything
   else is AMBIGUOUS or NO_MATCH, and nothing is attached.
3. Search ZoomInfo contacts at that company for the POC titles (free; no emails).
4. Enrich (one credit per record) only the strongest POC candidates whose email
   would fill a gap - missing, or weaker than PERSONAL - capped per company.
5. Merge with the public contacts: the same person (same email, or same first
   and last name at this company) is one contact. ZoomInfo fills missing fields
   and upgrades a weak email; a PERSONAL public email is never replaced. Every
   field ZoomInfo supplied is recorded in ``field_provenance``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from nexbase.config import Settings, get_settings
from nexbase.contacts.extraction import infer_priority
from nexbase.core.geo import us_state_of
from nexbase.email.discovery import (
    EMAIL_CLASS_STRENGTH,
    PERSONAL,
    classify_email,
)
from nexbase.enrichment.zoominfo import ZoomInfoCompany, ZoomInfoProvider
from nexbase.logging_setup import get_logger
from nexbase.pipeline.normalize import extract_host, normalize_company_name, registrable_domain

#: The Master Plan's POC titles, as ZoomInfo ``jobTitle`` search terms.
POC_SEARCH_TITLES = [
    "Owner", "CEO", "President", "Managing Partner",
    "COO", "VP Operations", "Director of Operations", "General Manager",
    "HR Director", "HR Manager", "Head of HR", "Head of People", "Talent Acquisition",
    "Plant Manager", "Operations Manager",
]

PUBLIC, ZOOMINFO, PUBLIC_AND_ZOOMINFO = "PUBLIC", "ZOOMINFO", "PUBLIC+ZOOMINFO"
_ORIGIN_RANK = {PUBLIC: 0, PUBLIC_AND_ZOOMINFO: 0, ZOOMINFO: 1}
_NAME_NOISE = frozenset({"jr", "sr", "ii", "iii", "iv", "mr", "mrs", "ms", "dr"})


def person_key(name: str | None) -> tuple[str, str] | None:
    """(first, last) name tokens; middle initials and suffixes ignored."""
    tokens = [t for t in re.findall(r"[a-z]+", (name or "").lower())
              if len(t) > 1 and t not in _NAME_NOISE]
    return (tokens[0], tokens[-1]) if len(tokens) >= 2 else None


def _email_strength(email: str | None, domain: str | None) -> int:
    return EMAIL_CLASS_STRENGTH[classify_email(email, domain)] if email else -1


@dataclass
class CompanyEnrichment:
    status: str
    match_basis: str | None = None
    zoominfo_company: ZoomInfoCompany | None = None
    credits: float = 0.0
    contacts_added: int = 0
    contacts_upgraded: int = 0
    #: ZoomInfo-supplied emails, for the email classification pass.
    zoominfo_emails: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        company = self.zoominfo_company
        return {"status": self.status, "match_basis": self.match_basis,
                "zoominfo_company_id": getattr(company, "id", None),
                "zoominfo_company_name": getattr(company, "name", None),
                "credits": self.credits, "contacts_added": self.contacts_added,
                "contacts_upgraded": self.contacts_upgraded}


class ZoomInfoEnrichment:
    def __init__(self, repo, settings: Settings | None = None, logger=None,
                 provider: ZoomInfoProvider | None = None) -> None:
        self.repo = repo
        self.settings = settings or get_settings()
        self.log = logger or get_logger("nexbase.enrichment.zoominfo_stage")
        self.provider = provider or ZoomInfoProvider(self.settings, self.log)

    # ------------------------------------------------------------------
    def enrich_company(self, fc, lead, company_id) -> CompanyEnrichment:
        """Enrich one QUALIFIED company's lead in place."""
        domain = lead.domain or None
        if not self.provider.is_configured():
            return CompanyEnrichment("NOT_CONFIGURED")
        if self._strong_public_contacts(lead.contacts, domain) >= self.settings.contacts_target:
            return CompanyEnrichment("SKIPPED_STRONG_PUBLIC_DATA")
        cached = self._cached_contacts(company_id)
        if cached is not None:
            result = CompanyEnrichment("CACHED")
            self._merge(lead, cached, domain, result, company=None, basis="CACHED")
            return result

        calls_before = len(self.provider.calls)
        result = self._enrich_live(fc, lead, company_id, domain)
        new_calls = self.provider.calls[calls_before:]
        result.credits = sum(c.credit_cost for c in new_calls)
        for call in new_calls:
            self.repo.insert_enrichment_log({
                "company_id": company_id, "provider": "ZOOMINFO", "endpoint": call.endpoint,
                "payload": call.payload, "status": call.status, "billable": call.billable,
                "credit_cost": call.credit_cost,
                "result": {"records": call.records, "error": call.error,
                           "company_status": result.status, "match_basis": result.match_basis},
            })
        return result

    # ------------------------------------------------------------------
    def _enrich_live(self, fc, lead, company_id, domain) -> CompanyEnrichment:
        name = lead.display_name or lead.company_name
        candidates = self.provider.match_companies(name, domain)
        company, basis, status = self._match(candidates, fc, domain)
        if company is None:
            self.log.info("zoominfo_company_not_attached", company=name, status=status,
                          candidates=len(candidates))
            return CompanyEnrichment(status)
        result = CompanyEnrichment("ENRICHED", match_basis=basis, zoominfo_company=company)
        self._record_company_evidence(company_id, company, basis)

        previews = [
            row for row in self.provider.search_contacts(company.id, POC_SEARCH_TITLES)
            if infer_priority(row.get("jobTitle")) is not None
            and _same_company((row.get("company") or {}).get("id"), company.id)
        ]
        to_enrich = self._worth_enriching(previews, lead.contacts, domain)
        enriched = self.provider.enrich_contacts([row["id"] for row in to_enrich])
        enriched_ids = {c.raw.get("zoominfo_person_id") for c in enriched}

        found: list[dict] = []
        for contact in enriched:
            if not _same_company(contact.raw.get("zoominfo_company_id"), company.id):
                self.log.info("zoominfo_contact_other_company_dropped",
                              person_id=contact.raw.get("zoominfo_person_id"))
                continue
            found.append(_contact_dict(contact.name, contact.title, contact.email,
                                       contact.profile_url, contact.raw,
                                       "zoominfo_enrich_contact", self.provider._url("enrich/contact")))
        # Search previews not enriched still name a POC at the company, for free.
        for row in previews:
            if row.get("id") in enriched_ids:
                continue
            name_ = " ".join(p for p in (row.get("firstName"), row.get("lastName")) if p) or None
            found.append(_contact_dict(name_, row.get("jobTitle"), None, None, {
                "zoominfo_person_id": row.get("id"),
                "zoominfo_company_id": (row.get("company") or {}).get("id"),
                "contact_accuracy_score": row.get("contactAccuracyScore"),
                "has_email": row.get("hasEmail"),
            }, "zoominfo_search_contact", self.provider._url("search/contact")))

        self._merge(lead, found, domain, result, company=company, basis=basis)
        self._persist(company_id, lead)
        return result

    def _match(self, candidates: list[ZoomInfoCompany], fc,
               domain: str | None) -> tuple[ZoomInfoCompany | None, str | None, str]:
        """The single ZoomInfo company this employer is, or why there is none."""
        if not candidates:
            return None, None, "NO_MATCH"
        if domain:
            root = _root(domain)
            on_domain = {c.id: c for c in candidates
                         if root in {_root(d) for d in [c.website or "", *c.domains]}}
            if len(on_domain) == 1:
                return next(iter(on_domain.values())), "DOMAIN", "ENRICHED"
            return None, None, "AMBIGUOUS" if on_domain else "DOMAIN_MISMATCH"
        name = fc.company.company_name_normalized
        states = set(fc.company.states)
        same = {c.id: c for c in candidates
                if normalize_company_name(c.name) == name
                and (not states or us_state_of(c.state or "") in states)}
        if len(same) == 1 and states:
            return next(iter(same.values())), "NAME_AND_STATE", "ENRICHED"
        return None, None, "AMBIGUOUS" if same or len(candidates) > 1 else "NO_MATCH"

    def _worth_enriching(self, previews: list[dict], contacts: list[dict],
                         domain: str | None) -> list[dict]:
        """Previews whose enrichment would fill a gap, strongest POC first."""
        by_person = {person_key(c.get("name")): c for c in contacts if person_key(c.get("name"))}
        wanted = []
        for row in previews:
            if not row.get("hasEmail"):
                continue
            key = person_key(" ".join(p for p in (row.get("firstName"), row.get("lastName")) if p))
            public = by_person.get(key)
            if public and _email_strength(public.get("email"), domain) >= EMAIL_CLASS_STRENGTH[PERSONAL]:
                continue  # already has a PERSONAL company-domain email: do not re-enrich
            wanted.append(row)
        wanted.sort(key=lambda r: (infer_priority(r.get("jobTitle")),
                                   -(r.get("contactAccuracyScore") or 0)))
        return wanted[: self.settings.zoominfo_max_contact_enrich_per_company]

    # ------------------------------------------------------------------
    def _merge(self, lead, found: list[dict], domain, result: CompanyEnrichment,
               company, basis) -> None:
        contacts = [dict(c) for c in lead.contacts]
        for z in found:
            match = next((c for c in contacts if _same_person(c, z)), None)
            if match is None:
                if not z.get("title_priority"):
                    continue
                contacts.append(z)
                result.contacts_added += 1
            elif self._fill(match, z, domain):
                result.contacts_upgraded += 1
            if z.get("email"):
                result.zoominfo_emails.append({
                    "email": z["email"], "source": "zoominfo", "source_type": ZOOMINFO,
                    "evidence_url": z.get("source_url"),
                    "extraction_method": z.get("extraction"), "discovery_stage": "ENRICHMENT",
                    "contact_name": z.get("name"), "contact_title": z.get("title"),
                })

        contacts.sort(key=lambda c: (
            c.get("title_priority") or 9,
            -_email_strength(c.get("email"), domain),
            _ORIGIN_RANK.get(c.get("origin"), 0),
            -(c.get("rank_score") or 0),
        ))
        lead.contacts = contacts[: self.settings.contacts_max]

    @staticmethod
    def _fill(public: dict, z: dict, domain) -> bool:
        """Fill missing fields and upgrade a weaker email; never downgrade."""
        provenance = public.setdefault("field_provenance", {})
        changed = False
        source = {"source": "zoominfo", "source_type": ZOOMINFO,
                  "evidence_url": z.get("source_url"), "extraction_method": z.get("extraction"),
                  "zoominfo_person_id": z.get("zoominfo_person_id"),
                  "contact_accuracy_score": z.get("contact_accuracy_score")}
        if z.get("email") and _email_strength(z["email"], domain) > _email_strength(public.get("email"), domain):
            if public.get("email"):
                public.setdefault("replaced_emails", []).append(
                    {"email": public["email"],
                     "email_class": classify_email(public["email"], domain)})
            public["email"] = z["email"]
            provenance["email"] = source
            changed = True
        if z.get("profile_url") and not public.get("profile_url"):
            public["profile_url"] = z["profile_url"]
            provenance["profile_url"] = source
            changed = True
        public["zoominfo_person_id"] = z.get("zoominfo_person_id")
        if changed or z.get("zoominfo_person_id"):
            public["origin"] = PUBLIC_AND_ZOOMINFO
        return changed

    @staticmethod
    def _strong_public_contacts(contacts: list[dict], domain) -> int:
        return sum(1 for c in contacts if c.get("title_priority") and c.get("email")
                   and classify_email(c["email"], domain) == PERSONAL)

    # ------------------------------------------------------------------
    def _cached_contacts(self, company_id) -> list[dict] | None:
        """ZoomInfo contacts stored within the refresh window, or None to call ZoomInfo."""
        if not getattr(self.repo, "configured", False) or company_id is None:
            return None
        logs = self.repo.select(
            "enrichment_logs", "created_at, status",
            filters={"company_id": company_id, "provider": ZOOMINFO},
            order_by="created_at", limit=1)
        if not logs:
            return None
        created = _parse_time(logs[0].get("created_at"))
        if created is None or datetime.now(timezone.utc) - created > timedelta(
                days=self.settings.zoominfo_refresh_days):
            return None
        rows = self.repo.select(
            "contacts", "id, name, title, title_priority, email, profile_url, raw_payload",
            filters={"company_id": company_id, "source_type": ZOOMINFO})
        return [{**(row.get("raw_payload") or {}), **{k: row.get(k) for k in (
            "id", "name", "title", "title_priority", "email", "profile_url")},
            "origin": ZOOMINFO} for row in rows]

    def _record_company_evidence(self, company_id, company: ZoomInfoCompany, basis: str) -> None:
        """ZoomInfo company facts as evidence only: identity and qualification stay as they are."""
        self.repo.insert_evidence({
            "record_type": "COMPANY", "record_id": company_id, "key": "zoominfo_company_match",
            "value": str(company.id), "url": self.provider._url("enrich/company"),
            "source_type": ZOOMINFO, "source_priority": 4,
            "raw_payload": {"match_basis": basis, **company.raw},
        })

    def _persist(self, company_id, lead) -> None:
        for contact in lead.contacts:
            if contact.get("origin") == PUBLIC:
                continue
            row = {
                "company_id": company_id, "name": contact.get("name"),
                "title": contact.get("title"), "title_priority": contact.get("title_priority"),
                "email": contact.get("email"), "profile_url": contact.get("profile_url"),
                "discovery_stage": contact.get("discovery_stage"),
                "source_type": contact.get("source_type"),
                "source_priority": contact.get("source_priority"),
                "raw_payload": {k: contact.get(k) for k in (
                    "origin", "field_provenance", "replaced_emails", "zoominfo_person_id",
                    "zoominfo_company_id", "contact_accuracy_score", "extraction", "source_url")},
            }
            if contact.get("origin") == PUBLIC_AND_ZOOMINFO and contact.get("id"):
                # A public contact keeps its own row and provenance; only the
                # fields ZoomInfo filled change, each backed by an evidence row.
                self.repo.update_contact(contact["id"], {
                    k: row[k] for k in ("email", "profile_url")})
            else:
                contact["id"] = self.repo.upsert_contact(row)
            for field_name, source in (contact.get("field_provenance") or {}).items():
                self.repo.insert_evidence({
                    "record_type": "CONTACT", "record_id": contact.get("id"),
                    "key": f"zoominfo_{field_name}", "value": str(contact.get(field_name)),
                    "url": source.get("evidence_url"), "source_type": ZOOMINFO,
                    "source_priority": 4, "raw_payload": source,
                })


def _contact_dict(name, title, email, profile_url, raw: dict, method: str, endpoint: str) -> dict:
    source = {"source": "zoominfo", "source_type": ZOOMINFO, "evidence_url": endpoint,
              "extraction_method": method,
              "zoominfo_person_id": raw.get("zoominfo_person_id"),
              "contact_accuracy_score": raw.get("contact_accuracy_score")}
    provenance = {f: source for f, v in (("name", name), ("title", title), ("email", email),
                                          ("profile_url", profile_url)) if v}
    return {
        "id": None, "name": name, "title": title, "title_priority": infer_priority(title),
        "email": email, "profile_url": profile_url, "rank_score": None,
        "discovery_stage": "ENRICHMENT", "source_type": ZOOMINFO, "source_priority": 4,
        "source": "zoominfo", "source_url": endpoint, "extraction": method,
        "origin": ZOOMINFO, "field_provenance": provenance,
        "zoominfo_person_id": raw.get("zoominfo_person_id"),
        "zoominfo_company_id": raw.get("zoominfo_company_id"),
        "contact_accuracy_score": raw.get("contact_accuracy_score"),
    }


def _root(url_or_host: str | None) -> str:
    return registrable_domain(extract_host(url_or_host or ""))


def _same_company(value: Any, company_id: Any) -> bool:
    """True unless ZoomInfo named a different company id."""
    return value is None or str(value) == str(company_id)


def _same_person(a: dict, b: dict) -> bool:
    if a.get("email") and b.get("email") and a["email"].lower() == b["email"].lower():
        return True
    if a.get("zoominfo_person_id") and a.get("zoominfo_person_id") == b.get("zoominfo_person_id"):
        return True
    key = person_key(a.get("name"))
    return key is not None and key == person_key(b.get("name"))


def _parse_time(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
