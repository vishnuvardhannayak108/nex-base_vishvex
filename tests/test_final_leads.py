"""Phase 8: POC ranking, email verification, Final Lead, export.

ZeroBounce responses are fixtures in the shapes its documentation gives for
``/v2/validate`` (``status``/``sub_status``; ``{"error": ...}`` on failure). No
ZeroBounce call was made.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone

import httpx

from nexbase.contacts.ranking import rank_pocs
from nexbase.email.verification import EmailVerifier, VerificationResult, VerificationStage
from nexbase.export import (
    NO_VERIFIED_POC_EMAIL,
    PENDING_VERIFICATION,
    READY,
    csv_rows,
    export_leads_csv,
    final_lead,
    iter_final_leads,
    lead_status,
)
from tests.conftest import RecordingRepo

DOMAIN = "acme-mfg.com"


def _poc(name, title, email=None, origin="PUBLIC", **extra):
    return {"id": f"id-{name}", "name": name, "title": title, "email": email, "origin": origin,
            **extra}


# ===========================================================================
# POC ranking
# ===========================================================================
def test_pocs_rank_strictly_by_plan_tier():
    pocs = rank_pocs([
        _poc("Pat Plant", "Plant Manager"), _poc("Hana Hr", "HR Director"),
        _poc("Olga Ops", "VP Operations"), _poc("Carl Chief", "CEO"),
    ], DOMAIN)
    assert [p["title_priority"] for p in pocs] == [1, 2, 3, 4]
    assert [p["poc_rank"] for p in pocs] == [1, 2, 3, 4]


def test_within_a_tier_a_personal_email_beats_role_and_none():
    pocs = rank_pocs([
        _poc("Ann None", "President"),
        _poc("Bob Role", "Owner", "info@acme-mfg.com"),
        _poc("Cat Personal", "CEO", "cat@acme-mfg.com"),
    ], DOMAIN)
    assert [p["name"] for p in pocs] == ["Cat Personal", "Bob Role", "Ann None"]


def test_quality_over_quota_drops_non_pocs_and_never_pads():
    pocs = rank_pocs([
        _poc("Sam Sales", "Sales Representative"), _poc(None, "CEO"),
        _poc("  ", "COO"), _poc("Real Person", "General Manager"),
    ], DOMAIN, limit=6)
    assert [p["name"] for p in pocs] == ["Real Person"]


def test_ranking_recomputes_the_tier_from_the_title_and_respects_the_limit():
    pocs = rank_pocs([_poc("A B", "Plant Manager", title_priority=1),
                      _poc("C D", "COO"), _poc("E F", "CEO")], DOMAIN, limit=2)
    assert [(p["name"], p["title_priority"]) for p in pocs] == [("E F", 1), ("C D", 2)]


def test_equal_quality_keeps_public_evidence_before_providers():
    pocs = rank_pocs([_poc("Zed Zoom", "CEO", "zed@acme-mfg.com", origin="ZOOMINFO"),
                      _poc("Pia Pub", "CEO", "pia@acme-mfg.com")], DOMAIN)
    assert [p["name"] for p in pocs] == ["Pia Pub", "Zed Zoom"]


# ===========================================================================
# ZeroBounce verifier (documented API only)
# ===========================================================================
def _verifier(settings, handler):
    settings.zerobounce_api_key = "k"
    return EmailVerifier(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))


def _json(payload, code=200):
    return lambda request: httpx.Response(code, json=payload)


def test_not_configured_is_pending_and_makes_no_call(settings):
    result = EmailVerifier(settings).verify("jane@acme-mfg.com")
    assert (result.status, result.reason, result.called) == ("PENDING", "NOT_CONFIGURED", False)


def test_documented_statuses_map_and_unknown_is_not_billable(settings):
    cases = {"valid": "VALID", "invalid": "INVALID", "catch-all": "RISKY",
             "do_not_mail": "RISKY", "spamtrap": "RISKY", "abuse": "RISKY", "unknown": "UNKNOWN"}
    for provider_status, expected in cases.items():
        result = _verifier(settings, _json({"status": provider_status,
                                            "sub_status": "role_based"})).verify("j@acme-mfg.com")
        assert result.status == expected
        assert result.provider_status == provider_status and result.sub_status == "role_based"
        assert result.billable is (provider_status != "unknown")


def test_provider_failures_never_become_a_verdict(settings):
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    cases = [
        (_json({"error": "Invalid API Key or your account ran out of credits"}),
         "PROVIDER_UNAVAILABLE"),
        (_json({}, 401), "PROVIDER_UNAVAILABLE"),
        (_json({}, 429), "PROVIDER_RATE_LIMITED"),
        (_json({}, 500), "PROVIDER_ERROR:HTTPStatusError"),
        (timeout, "PROVIDER_TIMEOUT"),
        (_json({"status": "something_new"}), "UNRECOGNIZED_STATUS:something_new"),
    ]
    for handler, reason in cases:
        result = _verifier(settings, handler).verify("j@acme-mfg.com")
        assert (result.status, result.reason, result.billable) == ("PENDING", reason, False)


# ===========================================================================
# Verification stage: gate, class filter, budget, cache, persistence
# ===========================================================================
class FakeVerifier:
    def __init__(self, *statuses, configured=True):
        self.statuses = list(statuses)
        self.configured = configured
        self.asked = []

    def is_configured(self):
        return self.configured

    def verify(self, email):
        self.asked.append(email)
        status = self.statuses.pop(0) if self.statuses else "VALID"
        if status.startswith("PROVIDER_"):
            return VerificationResult(email, "PENDING", status, called=True)
        return VerificationResult(email, status, None, status.lower(), None,
                                  billable=status != "UNKNOWN", called=True)


def _stage(settings, verifier, repo=None, enabled=True, **config):
    settings.email_verification_enabled = enabled
    for key, value in config.items():
        setattr(settings, key, value)
    return VerificationStage(repo if repo is not None else RecordingRepo(), settings, verifier)


def test_verification_is_off_by_default(settings):
    verifier = FakeVerifier()
    contacts = [_poc("Cat", "CEO", "cat@acme-mfg.com")]
    _stage(settings, verifier, enabled=False).verify_lead(contacts, DOMAIN, "co-1")
    assert verifier.asked == []
    assert contacts[0]["verification_status"] == "PENDING"
    assert contacts[0]["verification"]["reason"] == "DISABLED"


def test_only_company_domain_mailboxes_are_sent_for_verification(settings):
    verifier = FakeVerifier()
    repo = RecordingRepo()
    contacts = [_poc("Cat", "CEO", "cat@acme-mfg.com"), _poc("Hr", "HR Manager", "hr@acme-mfg.com"),
                _poc("Gus", "COO", "gus@gmail.com"), _poc("Ida", "Owner", "ida@other.com"),
                _poc("Ned", "GM")]
    _stage(settings, verifier, repo).verify_lead(contacts, DOMAIN, "co-1")
    assert verifier.asked == ["cat@acme-mfg.com", "hr@acme-mfg.com"]
    assert [c.get("verification_status") for c in contacts] == [
        "VALID", "VALID", "SKIPPED", "SKIPPED", None]
    assert contacts[2]["verification"]["reason"] == "EMAIL_CLASS:EXTERNAL_UNVERIFIED"
    assert contacts[3]["verification"]["reason"] == "EMAIL_CLASS:DOMAIN_MISMATCH"


def test_results_are_logged_and_written_to_the_contact_with_provenance(settings):
    repo = RecordingRepo()
    contacts = [_poc("Cat", "CEO", "Cat@Acme-Mfg.com")]
    _stage(settings, FakeVerifier("INVALID"), repo).verify_lead(contacts, DOMAIN, "co-1")
    [log] = repo.calls["email_verification"]
    assert log["email"] == "cat@acme-mfg.com" and log["provider"] == "ZEROBOUNCE"
    assert log["status"] == "INVALID" and log["contact_id"] == "id-Cat"
    assert log["raw_result"]["called"] is True and log["raw_result"]["at"]
    assert repo.calls["contact_update"] == [("id-Cat", {"verification_status": "INVALID"})]


def test_a_failed_call_is_logged_but_not_written_as_a_verdict(settings):
    repo = RecordingRepo()
    contacts = [_poc("Cat", "CEO", "cat@acme-mfg.com")]
    _stage(settings, FakeVerifier("PROVIDER_TIMEOUT"), repo).verify_lead(contacts, DOMAIN, "co-1")
    assert repo.calls["email_verification"][0]["status"] == "PENDING"
    assert "contact_update" not in repo.calls


def test_budget_caps_paid_calls_per_run(settings):
    verifier = FakeVerifier()
    stage = _stage(settings, verifier, email_verification_max_per_run=1)
    contacts = [_poc("Cat", "CEO", "cat@acme-mfg.com"), _poc("Dan", "COO", "dan@acme-mfg.com")]
    stage.verify_lead(contacts, DOMAIN, "co-1")
    assert len(verifier.asked) == 1
    assert contacts[1]["verification"]["reason"] == "BUDGET_EXHAUSTED"


def test_unavailable_provider_stops_calls_for_the_rest_of_the_run(settings):
    verifier = FakeVerifier("PROVIDER_UNAVAILABLE")
    stage = _stage(settings, verifier)
    contacts = [_poc("Cat", "CEO", "cat@acme-mfg.com"), _poc("Dan", "COO", "dan@acme-mfg.com")]
    stage.verify_lead(contacts, DOMAIN, "co-1")
    assert verifier.asked == ["cat@acme-mfg.com"]
    assert contacts[1]["verification"]["reason"] == "PROVIDER_UNAVAILABLE"


def test_the_same_email_is_verified_once_per_run(settings):
    verifier = FakeVerifier()
    stage = _stage(settings, verifier)
    stage.verify_lead([_poc("Cat", "CEO", "cat@acme-mfg.com")], DOMAIN, "co-1")
    stage.verify_lead([_poc("Cat", "CEO", "cat@acme-mfg.com")], DOMAIN, "co-1")
    assert verifier.asked == ["cat@acme-mfg.com"]


class CacheRepo(RecordingRepo):
    def __init__(self, status, age_days):
        super().__init__()
        created = datetime.now(timezone.utc) - timedelta(days=age_days)
        self.row = {"status": status, "created_at": created.isoformat(),
                    "raw_result": {"provider_status": status.lower(), "sub_status": None}}

    def select(self, table, *args, **kwargs):
        return [self.row] if table == "email_verification_logs" else []


def test_a_recent_final_verdict_is_reused_without_a_call(settings):
    verifier = FakeVerifier()
    contacts = [_poc("Cat", "CEO", "cat@acme-mfg.com")]
    _stage(settings, verifier, CacheRepo("VALID", 3)).verify_lead(contacts, DOMAIN, "co-1")
    assert verifier.asked == []
    assert contacts[0]["verification_status"] == "VALID"
    assert contacts[0]["verification"]["reason"] == "CACHED"


def test_stale_or_unknown_verdicts_are_checked_again(settings):
    for status, age in (("VALID", 31), ("UNKNOWN", 1)):
        verifier = FakeVerifier()
        _stage(settings, verifier, CacheRepo(status, age)).verify_lead(
            [_poc("Cat", "CEO", "cat@acme-mfg.com")], DOMAIN, "co-1")
        assert verifier.asked == ["cat@acme-mfg.com"], status


# ===========================================================================
# Lead status
# ===========================================================================
def test_lead_status_needs_a_valid_personal_poc_email():
    valid_personal = _poc("Cat", "CEO", "cat@acme-mfg.com", verification_status="VALID")
    pending = _poc("Dan", "COO", "dan@acme-mfg.com", verification_status="PENDING")
    invalid = _poc("Eve", "COO", "eve@acme-mfg.com", verification_status="INVALID")
    valid_role = _poc("Hr", "HR Manager", "hr@acme-mfg.com", verification_status="VALID")
    assert lead_status([invalid, valid_personal], DOMAIN) == READY
    assert lead_status([invalid, pending], DOMAIN) == PENDING_VERIFICATION
    assert lead_status([invalid, valid_role], DOMAIN) == NO_VERIFIED_POC_EMAIL
    assert lead_status([], DOMAIN) == NO_VERIFIED_POC_EMAIL


# ===========================================================================
# Final Lead record + CSV export + API
# ===========================================================================
class StoreRepo(RecordingRepo):
    """A tiny in-memory table store answering ``select`` with equality filters."""

    def __init__(self, tables):
        super().__init__()
        self.tables = tables

    def select(self, table, columns="*", limit=None, filters=None, order_by=None, **_):
        rows = [r for r in self.tables.get(table, [])
                if all(r.get(k) == v for k, v in (filters or {}).items())]
        return rows[:limit] if limit else rows


def _store():
    company = {"id": "co-1", "display_name": "Acme Manufacturing", "domain": DOMAIN,
               "website": f"https://{DOMAIN}", "qualification_status": "QUALIFIED",
               "qualification_score": 72, "qualification_reasons": ["SIZE_ELIGIBLE"],
               "review_flags": [], "hiring_intensity": 2, "employee_size_min": 50,
               "employee_size_max": 200, "domain_confidence": "HIGH"}
    other = {"id": "co-2", "display_name": "Nobody Inc", "domain": "nobody.com",
             "qualification_status": "QUALIFIED"}
    return StoreRepo({
        "companies": [company, other],
        "contacts": [
            {"id": "k-1", "company_id": "co-1", "name": "Pat Plant", "title": "Plant Manager",
             "title_priority": 4, "email": "pat@acme-mfg.com", "verification_status": "PENDING",
             "source_type": "COMPANY_WEBSITE", "discovery_stage": "PUBLIC_WEB", "raw_payload": {}},
            {"id": "k-2", "company_id": "co-1", "name": "Carl Chief", "title": "CEO",
             "title_priority": 1, "email": "carl@acme-mfg.com", "verification_status": "VALID",
             "source_type": "ZOOMINFO", "discovery_stage": "ENRICHMENT",
             "raw_payload": {"origin": "ZOOMINFO", "field_provenance": {"email": {
                 "provider": "ZOOMINFO", "evidence_url": None}}}},
            {"id": "k-3", "company_id": "co-1", "name": "Sam Sales", "title": "Sales Rep",
             "title_priority": None, "email": None, "raw_payload": {}},
        ],
        "evidence": [
            {"record_type": "CONTACT", "record_id": "k-1", "key": "contact",
             "value": "Pat Plant | Plant Manager", "url": f"https://{DOMAIN}/team",
             "source_type": "COMPANY_WEBSITE"},
            {"record_type": "COMPANY", "record_id": "co-1", "key": "company_email",
             "value": "hr@acme-mfg.com", "url": f"https://{DOMAIN}/careers",
             "source_type": "COMPANY_WEBSITE",
             "raw_payload": {"email_type": "ROLE", "extraction_method": "mailto"}},
        ],
        "email_verification_logs": [
            {"company_id": "co-1", "email": "carl@acme-mfg.com", "status": "VALID",
             "created_at": "2026-09-17T00:00:00+00:00",
             "raw_result": {"provider": "ZEROBOUNCE", "provider_status": "valid",
                            "sub_status": None, "at": "2026-09-17T00:00:00+00:00"}},
        ],
        "jobs": [{"company_id": "co-1", "title": "Warehouse Manager",
                  "evidence_url": "https://www.indeed.com/viewjob?jk=1", "source_type": "JOB_BOARD"}],
        "enrichment_logs": [{"company_id": "co-1", "provider": "ZOOMINFO",
                             "status": "PROVIDER_SUCCESS", "credit_cost": 1}],
    })


def test_final_lead_carries_ranked_pocs_verification_and_evidence():
    repo = _store()
    lead = final_lead(repo, repo.tables["companies"][0])
    assert lead["lead_status"] == READY
    assert [p["name"] for p in lead["pocs"]] == ["Carl Chief", "Pat Plant"]
    carl, pat = lead["pocs"]
    assert carl["verification"]["provider_status"] == "valid" and carl["email_class"] == "PERSONAL"
    assert carl["field_provenance"]["email"]["provider"] == "ZOOMINFO"
    assert pat["verification"] is None and pat["evidence"][0]["url"] == f"https://{DOMAIN}/team"
    assert lead["company_emails"] == [{"email": "hr@acme-mfg.com", "email_class": "ROLE",
                                       "evidence_url": f"https://{DOMAIN}/careers",
                                       "source_type": "COMPANY_WEBSITE",
                                       "extraction_method": "mailto"}]
    assert lead["hiring_evidence"][0]["title"] == "Warehouse Manager"
    assert lead["enrichment"][0]["provider"] == "ZOOMINFO"
    assert lead["qualification"]["qualification_reasons"] == ["SIZE_ELIGIBLE"]


def test_export_writes_one_row_per_poc_and_one_for_a_lead_without_pocs(tmp_path):
    path = tmp_path / "leads.csv"
    assert export_leads_csv(str(path), _store()) == 3
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert [(r["company_name"], r["poc_rank"], r["contact_name"]) for r in rows] == [
        ("Acme Manufacturing", "1", "Carl Chief"), ("Acme Manufacturing", "2", "Pat Plant"),
        ("Nobody Inc", "", "")]
    assert rows[0]["lead_status"] == READY and rows[2]["lead_status"] == NO_VERIFIED_POC_EMAIL
    assert rows[0]["verification_status"] == "VALID" and rows[0]["contact_origin"] == "ZOOMINFO"
    assert rows[1]["contact_evidence_urls"] == f"https://{DOMAIN}/team"
    assert rows[0]["company_emails"] == "hr@acme-mfg.com (ROLE)"


def test_export_can_filter_by_lead_status(tmp_path):
    leads = list(iter_final_leads(_store(), lead_status_filter=READY))
    assert [lead["company"]["display_name"] for lead in leads] == ["Acme Manufacturing"]
    assert csv_rows(leads[0])[0]["job_evidence_urls"] == "https://www.indeed.com/viewjob?jk=1"


def test_api_leads_returns_final_lead_records():
    import nexbase.api.main as api

    payload = api.leads(status="QUALIFIED", lead_status=None, limit=1, offset=1, repo=_store())
    assert payload["count"] == 1
    assert payload["items"][0]["company"]["display_name"] == "Nobody Inc"
    assert set(payload["items"][0]) >= {"lead_status", "pocs", "hiring_evidence", "enrichment"}


# ===========================================================================
# Runner: ranking and verification after enrichment, QUALIFIED only
# ===========================================================================
def test_runner_ranks_verifies_and_sets_lead_status(settings, make_job, now):
    from tests.test_enrichment_waterfall import Stub, _out, _person_
    from tests.test_pipeline_e2e import StubAccess
    from nexbase.enrichment.base import PROVIDER_SUCCESS
    from nexbase.pipeline.runner import PipelineRunner

    settings.email_verification_enabled = True
    zoominfo = Stub("ZOOMINFO", _out("ZOOMINFO", PROVIDER_SUCCESS,
                                     _person_("Pat Plant", "Plant Manager", "pat@acme-mfg.com", pid="p1"),
                                     _person_("Carl Chief", "CEO", "carl@acme-mfg.com", pid="p2")))
    verifier = FakeVerifier("VALID", "INVALID")
    runner = PipelineRunner(settings=settings, repo=RecordingRepo(),
                            access=StubAccess({}, settings=settings),
                            enrichment_providers=[zoominfo], email_verifier=verifier)
    job = make_job(company="Acme Manufacturing", employees="51 to 200", industry="Manufacturing",
                   company_website=f"https://{DOMAIN}")
    report = runner.run(raw_jobs=[job], now=now)

    [lead] = report.qualified
    assert [c["name"] for c in lead.contacts] == ["Carl Chief", "Pat Plant"]
    assert verifier.asked == ["carl@acme-mfg.com", "pat@acme-mfg.com"]
    assert [c["verification_status"] for c in lead.contacts] == ["VALID", "INVALID"]
    assert lead.lead_status == READY and report.final_leads == {READY: 1}
    assert list(report.stages)[-3:] == ["enrichment", "poc_ranking", "email_verification"]
