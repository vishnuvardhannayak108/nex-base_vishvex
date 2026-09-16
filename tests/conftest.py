"""Shared fixtures. No test in this suite touches the network or a database."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nexbase.config import Settings
from nexbase.core.models import RawJob
from nexbase.db.repository import InertRepository


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        supabase_url="",
        supabase_service_role_key="",
        db_strict=False,
        zoominfo_api_key="",
        apollo_api_key="",
        zerobounce_api_key="",
    )


@pytest.fixture
def repo() -> InertRepository:
    return InertRepository()


@pytest.fixture
def make_job(now):
    """Factory for RawJob records with sensible defaults."""

    def _make(
        company="Acme Manufacturing Inc.",
        title="Plant Manager",
        days_old=1.0,
        source_type="JOB_BOARD",
        source_priority=2,
        source_site="indeed",
        company_website=None,
        company_url=None,
        description="",
        employees=None,
        industry=None,
        location="Columbus, OH",
        applicant_count=None,
        external_id=None,
        emails=None,
    ) -> RawJob:
        return RawJob(
            source_type=source_type,
            source_priority=source_priority,
            source_site=source_site,
            external_id=external_id or f"{company}-{title}-{days_old}",
            title=title,
            company_name=company,
            company_url=company_url,
            company_website=company_website,
            location=location,
            description=description,
            posted_at=now - timedelta(days=days_old),
            application_url=f"https://www.indeed.com/viewjob?jk={abs(hash((company, title)))}",
            apply_url=None,
            company_industry=industry,
            company_employee_count=employees,
            applicant_count=applicant_count,
            observed_emails=list(emails or []),
        )

    return _make


class FakeResponse:
    """Minimal stand-in for a Scrapling Response."""

    def __init__(self, html: str, status: int = 200):
        self.html_content = html
        self.status = status


class RecordingRepo(InertRepository):
    """Captures writes so tests can assert on persistence behaviour."""

    def __init__(self):
        self.calls: dict[str, list] = {}

    def _record(self, name, payload):
        self.calls.setdefault(name, []).append(payload)

    def upsert_company(self, data, on_conflict=None):
        self._record("company", data)
        return "company-1"

    def upsert_job(self, data):
        self._record("job", data)
        return f"job-{len(self.calls['job'])}"

    def upsert_contact(self, data):
        self._record("contact", data)
        return f"contact-{len(self.calls['contact'])}"

    def update_contact(self, contact_id, data):
        self._record("contact_update", (contact_id, data))

    def update_company(self, company_id, data):
        self._record("company_update", (company_id, data))

    def insert_evidence(self, data):
        self._record("evidence", data)
        return "ev-1"

    def insert_qualification_reason(self, data):
        self._record("rejection", data)
        return "rej-1"

    def insert_enrichment_log(self, data):
        self._record("enrichment_log", data)
        return "enr-1"

    def insert_email_verification(self, data):
        self._record("email_verification", data)
        return "ver-1"

    def insert_audit(self, data):
        self._record("audit", data)
        return "aud-1"

    def upsert_hiring_history(self, data):
        self._record("hiring_history", data)
        return "hh-1"

    def count_hiring_history(self, company_id):
        return len(self.calls.get("hiring_history", []))

    def find_company(self, normalized_domain, normalized_name):
        return None

    @property
    def configured(self):
        return True


@pytest.fixture
def recording_repo() -> RecordingRepo:
    return RecordingRepo()
