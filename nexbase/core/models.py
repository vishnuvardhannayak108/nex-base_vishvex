"""Domain models shared across pipeline stages."""
from __future__ import annotations

import html
import re

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class RawJob:
    """A job posting as discovered by a source, before normalization.

    Source tracking is recorded here and carried through every later stage.
    Company-level facts observed at discovery time (industry, employee count,
    the employer's *own* website) are carried too, so the qualification gate
    can evaluate size and industry without paying for enrichment.
    """

    source_type: str
    source_priority: int
    source_site: str | None = None
    external_id: str | None = None
    title: str | None = None
    company_name: str | None = None
    #: Whatever URL the source associated with the employer. May well be a
    #: job-board profile page (indeed.com/cmp/..., linkedin.com/company/...).
    company_url: str | None = None
    #: The employer's own website when the source distinguishes it. This is
    #: the only field that may be trusted for domain derivation.
    company_website: str | None = None
    location: str | None = None
    description: str | None = None
    posted_at: datetime | None = None
    application_url: str | None = None
    apply_url: str | None = None
    ats_platform: str | None = None
    country: str | None = None
    #: Employment type as the SOURCE reported it, e.g. "fulltime", "contract".
    #: Never inferred - an unstated type stays None and is treated as unknown.
    employment_type: str | None = None
    is_remote: bool | None = None
    # Company facts observed for free at discovery time.
    #: Industry the SOURCE reported for the employer. Observed evidence.
    company_industry: str | None = None
    #: Industry of the SEARCH that found this posting. This is discovery
    #: intent, NOT a fact about the company: a query for a warehousing role
    #: routinely surfaces breweries and pharma firms. Never treat it as
    #: observed evidence.
    search_industry: str | None = None
    company_employee_count: str | None = None
    company_revenue: str | None = None
    company_addresses: str | None = None
    # Brief: LinkedIn applicant-count signal.
    applicant_count: int | None = None
    # Emails the source itself surfaced (never guessed).
    observed_emails: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    #: Which registered source, handler and planned query produced this row.
    #: Stamped by the source registry; empty for injected rows.
    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Harvest addresses the posting itself published.

        JobSpy exposes an ``emails`` column, but the board adapters and the ATS
        dataset do not - an address written into an ATS or board description was
        being discarded before the pipeline ever saw it. Extracting here covers
        every source in one place. Only literal addresses are taken; nothing is
        constructed from a person's name or a domain.
        """
        seen: dict[str, None] = {}
        for email in self.observed_emails or []:
            address = str(email).strip().lower()
            if address:
                seen.setdefault(address, None)
        for address in extract_emails_from_text(self.description):
            seen.setdefault(address, None)
        self.observed_emails = list(seen)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RawJob":
        from nexbase.core.timeutils import coerce_datetime

        emails = data.get("observed_emails") or data.get("emails") or []
        if isinstance(emails, str):
            emails = [e.strip() for e in emails.split(",") if e.strip()]

        return cls(
            source_type=data.get("source_type", "UNKNOWN"),
            source_priority=int(data.get("source_priority", 3)),
            source_site=data.get("source_site") or data.get("site"),
            external_id=data.get("external_id") or data.get("id"),
            title=data.get("title"),
            company_name=data.get("company_name") or data.get("company"),
            company_url=data.get("company_url"),
            company_website=data.get("company_website") or data.get("company_url_direct"),
            location=data.get("location"),
            description=data.get("description"),
            posted_at=coerce_datetime(data.get("posted_at") or data.get("date_posted")),
            application_url=(
                data.get("application_url")
                or data.get("job_url_direct")
                or data.get("job_url")
            ),
            apply_url=data.get("apply_url") or data.get("job_url"),
            ats_platform=data.get("ats_platform") or data.get("ats_type"),
            country=data.get("country") or data.get("country_iso"),
            employment_type=data.get("employment_type") or data.get("job_type"),
            is_remote=data.get("is_remote"),
            company_industry=data.get("company_industry"),
            search_industry=data.get("search_industry"),
            company_employee_count=data.get("company_employee_count")
            or data.get("company_num_employees"),
            company_revenue=data.get("company_revenue"),
            company_addresses=data.get("company_addresses"),
            applicant_count=_coerce_int(data.get("applicant_count")),
            observed_emails=list(emails),
            raw=data.get("raw", data),
            provenance=dict(data.get("provenance") or {}),
        )


#: The standard Raw Job schema every source must meet. Company is not listed: a
#: posting without one is discarded by normalization, which records why.
REQUIRED_RAW_JOB_FIELDS = ("source_type", "source_site", "title", "application_url")
RAW_JOB_SOURCE_TYPES = frozenset({"JOB_BOARD", "ATS"})


def raw_job_problems(job, portal: str) -> list[str]:
    """Why ``job`` does not meet the Raw Job schema for ``portal``; [] when it does."""
    if not isinstance(job, RawJob):
        return ["NOT_A_RAW_JOB"]
    problems = [f"MISSING_{name.upper()}" for name in REQUIRED_RAW_JOB_FIELDS
                if not getattr(job, name)]
    if job.source_type and job.source_type not in RAW_JOB_SOURCE_TYPES:
        problems.append("UNKNOWN_SOURCE_TYPE")
    if job.source_site and job.source_site != portal:
        problems.append("SOURCE_SITE_MISMATCH")
    if job.posted_at is not None and not isinstance(job.posted_at, datetime):
        problems.append("POSTED_AT_NOT_DATETIME")
    return problems


#: Literal addresses only. Nothing here ever constructs or guesses one.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

#: File extensions that make a "match" an asset filename, not an address.
_NOT_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")


def extract_emails_from_text(text: str | None) -> list[str]:
    """Explicit email addresses published in a block of text, deduplicated.

    Case is normalised so the same address written two ways counts once. HTML
    entities are decoded first, because descriptions arrive both as plain text
    and as HTML depending on the source.
    """
    if not text:
        return []
    decoded = html.unescape(str(text))
    seen: dict[str, None] = {}
    for match in _EMAIL_RE.findall(decoded):
        address = match.strip().lower().rstrip(".,;:)")
        if address.endswith(_NOT_EMAIL_SUFFIXES):
            continue
        seen.setdefault(address, None)
    return list(seen)


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
