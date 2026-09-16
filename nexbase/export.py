"""Lead export (CSV).

Emits one row per contact with the company context, the qualification
decision and the hiring evidence behind it. This is the hand-off point: nothing
here, or anywhere in NexBase, sends email.
"""
from __future__ import annotations

import csv
from typing import Any, Iterable

COLUMNS = [
    # Company
    "company_name", "domain", "website", "location", "client_industry",
    "employee_size_min", "employee_size_max", "employee_size_source",
    "is_direct_employer", "internal_ta_verdict",
    # Qualification
    "qualification_status", "qualification_score", "qualification_reasons",
    "review_flags", "hiring_intensity",
    "persistent_hiring_runs", "min_applicant_count",
    # Contact
    "contact_name", "contact_title", "contact_priority", "contact_email",
    "contact_rank_score", "discovery_stage",
    "verification_status", "verification_confidence",
    # Evidence & provenance
    "evidence_url",
    "source_type", "source_priority", "first_seen_at", "last_seen_at",
]


def _company_rows(repo, status: str | None) -> list[dict[str, Any]]:
    filters = {"qualification_status": status} if status and status != "ALL" else None
    return repo.select("companies", "*", filters=filters, limit=10000)


def iter_lead_rows(repo, status: str | None = "QUALIFIED") -> Iterable[dict[str, Any]]:
    """Yield one flat row per contact (or one per company when it has none)."""
    companies = _company_rows(repo, status)
    if not companies:
        return

    for company in companies:
        company_id = company.get("id")
        contacts = repo.select(
            "contacts", "*", filters={"company_id": company_id}, limit=50
        )
        evidence_url = None
        jobs = repo.select("jobs", "evidence_url, is_fresh", filters={"company_id": company_id}, limit=10)
        for job in jobs:
            if job.get("evidence_url"):
                evidence_url = job["evidence_url"]
                break

        base = {
            "company_name": company.get("display_name") or company.get("normalized_name"),
            "domain": company.get("domain"),
            "website": company.get("website"),
            "location": company.get("location"),
            "client_industry": company.get("client_industry"),
            "employee_size_min": company.get("employee_size_min"),
            "employee_size_max": company.get("employee_size_max"),
            "employee_size_source": company.get("employee_size_source"),
            "is_direct_employer": company.get("is_direct_employer"),
            "internal_ta_verdict": company.get("internal_ta_verdict"),
            "qualification_status": company.get("qualification_status"),
            "qualification_score": company.get("qualification_score"),
            "qualification_reasons": _join(company.get("qualification_reasons")),
            "review_flags": _join(company.get("review_flags")),
            "hiring_intensity": company.get("hiring_intensity"),
            "persistent_hiring_runs": company.get("persistent_hiring_runs"),
            "min_applicant_count": company.get("min_applicant_count"),
            "evidence_url": evidence_url,
            "source_type": company.get("source_type"),
            "source_priority": company.get("source_priority"),
            "first_seen_at": company.get("first_seen_at"),
            "last_seen_at": company.get("last_seen_at"),
        }

        if not contacts:
            yield {**base, **{k: None for k in _CONTACT_COLUMNS}}
            continue

        for contact in sorted(contacts, key=lambda c: (c.get("title_priority") or 9)):
            yield {
                **base,
                "contact_name": contact.get("name"),
                "contact_title": contact.get("title"),
                "contact_priority": contact.get("title_priority"),
                "contact_email": contact.get("email"),
                "contact_rank_score": contact.get("rank_score"),
                "discovery_stage": contact.get("discovery_stage"),
                "verification_status": contact.get("verification_status"),
                "verification_confidence": contact.get("verification_confidence"),
            }


_CONTACT_COLUMNS = (
    "contact_name", "contact_title", "contact_priority", "contact_email",
    "contact_rank_score", "discovery_stage",
    "verification_status", "verification_confidence",
)


def _join(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value)
    return str(value)


def export_leads_csv(path: str, repo, status: str | None = "QUALIFIED") -> int:
    """Write lead rows to ``path``. Returns the row count."""
    rows = list(iter_lead_rows(repo, status))
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)
