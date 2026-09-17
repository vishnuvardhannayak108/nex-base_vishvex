"""Final Lead record and export (CSV + API).

A Final Lead is one company with its qualification, hiring evidence, ranked POCs
(each with email class, verification result and provenance), company mailboxes
and enrichment log. It is the hand-off point: nothing here, or anywhere in
NexBase, sends email.

``lead_status`` (Quality > Quota, never padded):

* ``READY``                 a ranked POC has a PERSONAL company-domain email
                            that verification returned VALID;
* ``PENDING_VERIFICATION``  a ranked POC has a PERSONAL email not yet verified
                            (PENDING or UNKNOWN);
* ``NO_VERIFIED_POC_EMAIL`` otherwise: no POC with a PERSONAL email, or every
                            one came back INVALID or RISKY.
"""
from __future__ import annotations

import csv
from typing import Any, Iterable

from nexbase.contacts.ranking import rank_pocs
from nexbase.core.enums import VerificationStatus
from nexbase.email.discovery import PERSONAL, classify_email

READY = "READY"
PENDING_VERIFICATION = "PENDING_VERIFICATION"
NO_VERIFIED_POC_EMAIL = "NO_VERIFIED_POC_EMAIL"
LEAD_STATUSES = (READY, PENDING_VERIFICATION, NO_VERIFIED_POC_EMAIL)
_UNSETTLED = {None, VerificationStatus.PENDING.value, VerificationStatus.UNKNOWN.value}


def lead_status(pocs: list[dict], domain: str | None) -> str:
    personal = [p for p in pocs
                if p.get("email") and classify_email(p["email"], domain) == PERSONAL]
    if any(p.get("verification_status") == VerificationStatus.VALID.value for p in personal):
        return READY
    if any(p.get("verification_status") in _UNSETTLED for p in personal):
        return PENDING_VERIFICATION
    return NO_VERIFIED_POC_EMAIL


# ---------------------------------------------------------------------------
# Final Lead from stored records
# ---------------------------------------------------------------------------
def final_lead(repo, company: dict, contacts_max: int | None = None) -> dict:
    company_id = company.get("id")
    domain = company.get("domain")
    by_company = {"company_id": company_id}

    verifications: dict[str, dict] = {}
    for row in repo.select("email_verification_logs", "email, status, raw_result, created_at",
                           filters=by_company, order_by="created_at", limit=1000):
        verifications.setdefault((row.get("email") or "").lower(), row)  # newest first

    pocs = []
    for row in repo.select("contacts", "*", filters=by_company, limit=100):
        raw = row.get("raw_payload") or {}
        email = row.get("email")
        log = verifications.get((email or "").lower()) or {}
        result = log.get("raw_result") or {}
        pocs.append({
            "id": row.get("id"), "name": row.get("name"), "title": row.get("title"),
            "title_priority": row.get("title_priority"), "email": email,
            "email_class": classify_email(email, domain) if email else None,
            "verification_status": row.get("verification_status"),
            "verification": {"status": log.get("status"), "provider": result.get("provider"),
                             "provider_status": result.get("provider_status"),
                             "sub_status": result.get("sub_status"),
                             "verified_at": result.get("at") or log.get("created_at")}
            if log else None,
            "profile_url": row.get("profile_url"), "rank_score": row.get("rank_score"),
            "discovery_stage": row.get("discovery_stage"), "source_type": row.get("source_type"),
            "origin": raw.get("origin") or "PUBLIC",
            "field_provenance": raw.get("field_provenance"),
            "evidence": [
                {k: e.get(k) for k in ("key", "value", "url", "source_type", "captured_at")}
                for e in repo.select("evidence", "key, value, url, source_type, captured_at",
                                     filters={"record_type": "CONTACT",
                                              "record_id": row.get("id")}, limit=50)],
        })
    pocs = rank_pocs(pocs, domain, contacts_max)

    company_emails = [
        {"email": e.get("value"), "email_class": (e.get("raw_payload") or {}).get("email_type"),
         "evidence_url": e.get("url"), "source_type": e.get("source_type"),
         "extraction_method": (e.get("raw_payload") or {}).get("extraction_method")}
        for e in repo.select("evidence", "value, url, source_type, raw_payload",
                             filters={"record_type": "COMPANY", "record_id": company_id,
                                      "key": "company_email"}, limit=200)]

    return {
        "lead_status": lead_status(pocs, domain),
        "company": {k: company.get(k) for k in (
            "id", "display_name", "normalized_name", "domain", "website", "location",
            "client_industry", "industry_source", "employee_size_min", "employee_size_max",
            "employee_size_source", "domain_confidence", "domain_source",
            "domain_evidence_url")},
        "qualification": {k: company.get(k) for k in (
            "qualification_status", "qualification_score", "qualification_reasons",
            "review_flags", "hiring_intensity", "min_applicant_count")},
        "hiring_evidence": repo.select(
            "jobs", "title, location, posting_date, age_days, evidence_url, source_type, "
            "source_site", filters=by_company, limit=50),
        "pocs": pocs,
        "company_emails": company_emails,
        "enrichment": repo.select(
            "enrichment_logs", "provider, endpoint, status, billable, credit_cost, created_at",
            filters=by_company, order_by="created_at", limit=100),
    }


def iter_final_leads(repo, status: str | None = "QUALIFIED", lead_status_filter: str | None = None,
                     contacts_max: int | None = None) -> Iterable[dict]:
    filters = {"qualification_status": status} if status and status != "ALL" else None
    for company in repo.select("companies", "*", filters=filters,
                               order_by="qualification_score", limit=10000):
        lead = final_lead(repo, company, contacts_max)
        if lead_status_filter is None or lead["lead_status"] == lead_status_filter:
            yield lead


# ---------------------------------------------------------------------------
# CSV: one row per ranked POC (one row for a lead with none)
# ---------------------------------------------------------------------------
COLUMNS = [
    "lead_status",
    # Company
    "company_name", "domain", "website", "location", "client_industry",
    "employee_size_min", "employee_size_max", "employee_size_source",
    "domain_confidence", "domain_source",
    # Qualification + hiring evidence
    "qualification_status", "qualification_score", "qualification_reasons", "review_flags",
    "hiring_intensity", "min_applicant_count", "job_titles", "job_evidence_urls",
    "company_emails",
    # POC
    "poc_rank", "contact_name", "contact_title", "contact_priority", "contact_email",
    "email_class", "verification_status", "verification_sub_status", "verified_at",
    "contact_source_type", "contact_discovery_stage", "contact_origin",
    "contact_evidence_urls", "contact_profile_url",
]


def csv_rows(lead: dict) -> list[dict[str, Any]]:
    company, qual = lead["company"], lead["qualification"]
    base = {
        "lead_status": lead["lead_status"],
        "company_name": company.get("display_name") or company.get("normalized_name"),
        **{k: company.get(k) for k in (
            "domain", "website", "location", "client_industry", "employee_size_min",
            "employee_size_max", "employee_size_source", "domain_confidence", "domain_source")},
        **{k: qual.get(k) for k in (
            "qualification_status", "qualification_score", "hiring_intensity",
            "min_applicant_count")},
        "qualification_reasons": _join(qual.get("qualification_reasons")),
        "review_flags": _join(qual.get("review_flags")),
        "job_titles": _join(sorted({j.get("title") for j in lead["hiring_evidence"]
                                    if j.get("title")})),
        "job_evidence_urls": _join([j["evidence_url"] for j in lead["hiring_evidence"]
                                    if j.get("evidence_url")]),
        "company_emails": _join([f"{e['email']} ({e.get('email_class')})"
                                 for e in lead["company_emails"] if e.get("email")]),
    }
    if not lead["pocs"]:
        return [base]
    rows = []
    for poc in lead["pocs"]:
        verification = poc.get("verification") or {}
        rows.append({
            **base,
            "poc_rank": poc.get("poc_rank"), "contact_name": poc.get("name"),
            "contact_title": poc.get("title"), "contact_priority": poc.get("title_priority"),
            "contact_email": poc.get("email"), "email_class": poc.get("email_class"),
            "verification_status": poc.get("verification_status"),
            "verification_sub_status": verification.get("sub_status"),
            "verified_at": verification.get("verified_at"),
            "contact_source_type": poc.get("source_type"),
            "contact_discovery_stage": poc.get("discovery_stage"),
            "contact_origin": poc.get("origin"),
            "contact_evidence_urls": _join(sorted({e["url"] for e in poc.get("evidence") or []
                                                   if e.get("url")})),
            "contact_profile_url": poc.get("profile_url"),
        })
    return rows


def _join(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value) or None
    return str(value)


def export_leads_csv(path: str, repo, status: str | None = "QUALIFIED",
                     lead_status_filter: str | None = None) -> int:
    """Write final lead rows to ``path``. Returns the row count."""
    count = 0
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for lead in iter_final_leads(repo, status, lead_status_filter):
            rows = csv_rows(lead)
            writer.writerows(rows)
            count += len(rows)
    return count
