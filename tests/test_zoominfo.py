"""Phase 7: email classification and ZoomInfo enrichment.

ZoomInfo responses are fixtures in the shapes ZoomInfo's Enterprise API
documentation publishes (api-docs.zoominfo.com): ``/authenticate`` -> ``jwt``;
``/enrich/company`` and ``/enrich/contact`` -> ``data.result[].data[]``;
``/search/contact`` -> ``data[]`` previews with ``hasEmail`` and no email.
No live ZoomInfo account was used.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
import pytest

from nexbase.email.discovery import (
    DOMAIN_MISMATCH,
    EXTERNAL_UNVERIFIED,
    PERSONAL,
    PORTAL_GENERATED,
    ROLE,
    EmailDiscovery,
    classify_email,
)
from nexbase.enrichment.zoominfo import ZoomInfoProvider
from nexbase.enrichment.zoominfo_stage import ZoomInfoEnrichment, person_key
from nexbase.pipeline.company_identity import prepare_companies
from nexbase.pipeline.runner import PipelineRunner
from tests.conftest import RecordingRepo
from tests.test_pipeline_e2e import StubAccess


# ===========================================================================
# Email classification
# ===========================================================================
@pytest.mark.parametrize("email,domain,expected", [
    ("jane.doe@acme.com", "acme.com", PERSONAL),
    ("jane@mail.acme.com", "acme.com", PERSONAL),            # subdomain of the employer
    ("hr@acme.com", "acme.com", ROLE),
    ("careers@acme.com", "acme.com", ROLE),
    ("info@acme.com", "acme.com", ROLE),
    ("apply@indeed.com", "acme.com", PORTAL_GENERATED),
    ("jobs@acme.bamboohr.com", "acme.com", PORTAL_GENERATED),
    ("ops@hirebridge.com", "acme.com", PORTAL_GENERATED),
    ("joe.acme@gmail.com", "acme.com", EXTERNAL_UNVERIFIED),
    ("jane.doe@acme.com", None, EXTERNAL_UNVERIFIED),        # employer domain unknown
    # Live 2026-09-17: postings on classiccollision.net listed classiccollision.com.
    ("hr@classiccollision.com", "classiccollision.net", DOMAIN_MISMATCH),
    ("recruiter@randstadusa.com", "acme.com", DOMAIN_MISMATCH),
])
def test_email_classes(email, domain, expected):
    assert classify_email(email, domain) == expected


def test_every_email_keeps_its_class_confidence_and_provenance():
    result = EmailDiscovery().discover(
        [{"name": "Jane Doe", "title": "CEO", "email": "jane.doe@acme.com",
          "source": "acme.com", "source_type": "COMPANY_WEBSITE",
          "source_url": "https://acme.com/team", "extraction": "jsonld",
          "discovery_stage": "PUBLIC_WEB"}],
        extra_emails=[
            {"email": "apply@indeed.com", "source": "indeed", "source_type": "JOB_BOARD",
             "evidence_url": "https://www.indeed.com/viewjob?jk=1",
             "extraction_method": "job_posting", "discovery_stage": "SAME_SOURCE"},
            {"email": "joe.acme@gmail.com", "source": "indeed", "source_type": "JOB_BOARD",
             "evidence_url": "https://www.indeed.com/viewjob?jk=2",
             "extraction_method": "job_posting", "discovery_stage": "SAME_SOURCE"},
        ],
        company_domain="acme.com",
    )
    rows = {o.email: o.as_dict() for o in result.observed}
    for row in rows.values():
        for key in ("email", "source", "source_type", "email_class", "confidence",
                    "evidence_url", "extraction_method"):
            assert row[key] is not None, (row["email"], key)
    assert rows["jane.doe@acme.com"]["email_class"] == PERSONAL
    assert rows["jane.doe@acme.com"]["confidence"] > rows["apply@indeed.com"]["confidence"]
    # Weak addresses are preserved as evidence, never counted as the employer's own.
    assert {o.email for o in result.low_confidence} == {"apply@indeed.com", "joe.acme@gmail.com"}
    assert result.preferred == ["jane.doe@acme.com"]


def test_only_weak_emails_is_its_own_status():
    result = EmailDiscovery().discover([], extra_emails=["apply@indeed.com"],
                                       company_domain="acme.com")
    assert result.status == "LOW_CONFIDENCE_EMAIL_FOUND"
    assert result.preferred == [] and len(result.observed) == 1


# ===========================================================================
# ZoomInfo API fixtures (documented shapes)
# ===========================================================================
@dataclass
class FakeZoomInfo:
    companies: list[dict] = field(default_factory=list)
    previews: list[dict] = field(default_factory=list)
    people: dict = field(default_factory=dict)
    fail: set = field(default_factory=set)
    requests: list = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.strip("/")
        body = json.loads(request.content or b"{}")
        self.requests.append((path, body, request.headers.get("authorization")))
        if path in self.fail:
            return httpx.Response(500, json={"error": "boom"})
        if path == "authenticate":
            return httpx.Response(200, json={"jwt": "jwt-123"})
        if path == "enrich/company":
            return httpx.Response(200, json={"success": True, "data": {
                "outputFields": [body["outputFields"]],
                "result": [{"input": body["matchCompanyInput"][0], "data": self.companies}]}})
        if path == "search/contact":
            return httpx.Response(200, json={"maxResults": 100, "totalResults": len(self.previews),
                                             "currentPage": 1, "data": self.previews})
        if path == "enrich/contact":
            return httpx.Response(200, json={"success": True, "data": {
                "outputFields": [body["outputFields"]],
                "result": [{"input": {"personid": i["personId"]},
                            "data": [self.people[i["personId"]]] if i["personId"] in self.people else []}
                           for i in body["matchPersonInput"]]}})
        return httpx.Response(404)

    def paths(self):
        return [p for p, _, _ in self.requests]


def _preview(pid, first, last, title, has_email=True, company_id=77, score=95):
    return {"id": pid, "firstName": first, "middleName": "", "lastName": last,
            "jobTitle": title, "contactAccuracyScore": score, "hasEmail": has_email,
            "company": {"id": company_id, "name": "Acme Manufacturing"}}


def _person(pid, first, last, title, email, company_id=77):
    return {"id": pid, "firstName": first, "lastName": last, "email": email, "jobTitle": title,
            "contactAccuracyScore": 96, "companyId": company_id,
            "externalUrls": [{"type": "linkedin.com", "url": f"https://www.linkedin.com/in/{first}{last}"}]}


ACME = {"id": 77, "name": "Acme Manufacturing", "website": "www.acme-mfg.com",
        "domainList": ["www.acme-mfg.com", "acme-mfg.com"], "employeeCount": 120,
        "primaryIndustry": "Manufacturing", "city": "Toledo", "state": "Ohio",
        "country": "United States"}


@pytest.fixture
def zi_settings(settings):
    settings.zoominfo_api_key = "user@acme.test:secret"
    settings.zoominfo_api_url = "https://api.zoominfo.com"
    return settings


def _provider(settings, fake):
    return ZoomInfoProvider(settings, transport=httpx.MockTransport(fake.handler))


# ===========================================================================
# Provider: documented requests only
# ===========================================================================
def test_the_provider_authenticates_once_and_sends_documented_requests(zi_settings):
    fake = FakeZoomInfo(companies=[ACME], previews=[_preview(1, "Jane", "Doe", "CEO")],
                        people={1: _person(1, "Jane", "Doe", "CEO", "jane.doe@acme-mfg.com")})
    provider = _provider(zi_settings, fake)

    [company] = provider.match_companies("Acme Manufacturing", "acme-mfg.com")
    [preview] = provider.search_contacts(company.id, ["CEO", "COO"])
    [person] = provider.enrich_contacts([preview["id"]])

    assert fake.paths() == ["authenticate", "enrich/company", "search/contact", "enrich/contact"]
    assert fake.requests[0][1] == {"username": "user@acme.test", "password": "secret"}
    assert all(auth == "Bearer jwt-123" for _, _, auth in fake.requests[1:])
    assert fake.requests[1][1]["matchCompanyInput"] == [
        {"companyWebsite": "http://www.acme-mfg.com", "companyName": "Acme Manufacturing"}]
    assert fake.requests[2][1]["jobTitle"] == "CEO OR COO"
    assert fake.requests[3][1]["matchPersonInput"] == [{"personId": 1}]
    assert "email" not in preview, "Contact Search never returns emails"
    assert (company.id, company.employee_count) == (77, 120)
    assert person.email == "jane.doe@acme-mfg.com"
    assert person.profile_url == "https://www.linkedin.com/in/JaneDoe"
    assert person.raw["zoominfo_company_id"] == 77
    assert [c.credit_cost for c in provider.calls] == [1.0, 0.0, 1.0]


def test_errors_and_no_match_charge_nothing(zi_settings):
    fake = FakeZoomInfo(companies=[], fail={"search/contact"})
    provider = _provider(zi_settings, fake)
    assert provider.match_companies("Acme", "acme-mfg.com") == []
    assert provider.search_contacts(77, ["CEO"]) == []
    no_match, error = provider.calls
    assert (no_match.status, no_match.credit_cost) == ("NO_MATCH", 0.0)
    assert (error.status, error.billable, error.credit_cost) == ("ERROR", False, 0.0)


# ===========================================================================
# Enrichment stage
# ===========================================================================
@dataclass
class Lead:
    company_name: str = "acme manufacturing"
    display_name: str = "Acme Manufacturing"
    domain: str | None = "acme-mfg.com"
    contacts: list = field(default_factory=list)
    observed_emails: list = field(default_factory=list)


def _fc(make_job, settings, now, **job):
    fields = dict(company="Acme Manufacturing", title="Welder", employees="51 to 200",
                  industry="Manufacturing", company_website="https://acme-mfg.com",
                  location="Toledo, OH")
    fields.update(job)
    return prepare_companies([make_job(**fields)], settings, now)[0][0]


def _public(name, title, priority, email=None, cid="c-1"):
    return {"id": cid, "name": name, "title": title, "title_priority": priority, "email": email,
            "profile_url": None, "rank_score": 80.0, "discovery_stage": "PUBLIC_WEB",
            "source_type": "COMPANY_WEBSITE", "source": "acme-mfg.com",
            "source_url": "https://acme-mfg.com/leadership", "extraction": "text", "origin": "PUBLIC"}


def _stage(settings, fake, repo=None):
    return ZoomInfoEnrichment(repo or RecordingRepo(), settings, provider=_provider(settings, fake))


def test_an_unconfigured_provider_is_never_called(settings, make_job, now):
    fake = FakeZoomInfo(companies=[ACME])
    result = _stage(settings, fake).enrich_company(_fc(make_job, settings, now), Lead(), "co-1")
    assert result.status == "NOT_CONFIGURED" and fake.requests == []


def test_strong_public_data_is_not_re_enriched(zi_settings, make_job, now):
    zi_settings.contacts_target = 2
    lead = Lead(contacts=[_public("Jane Doe", "CEO", 1, "jane.doe@acme-mfg.com"),
                          _public("Carl Ortiz", "COO", 2, "carl@acme-mfg.com", "c-2")])
    fake = FakeZoomInfo(companies=[ACME])
    result = _stage(zi_settings, fake).enrich_company(_fc(make_job, zi_settings, now), lead, "co-1")
    assert result.status == "SKIPPED_STRONG_PUBLIC_DATA" and fake.requests == []


def test_a_company_on_another_domain_is_not_attached(zi_settings, make_job, now):
    other = {**ACME, "id": 88, "website": "www.acme-holdings.com", "domainList": ["acme-holdings.com"]}
    fake = FakeZoomInfo(companies=[other], previews=[_preview(1, "Jane", "Doe", "CEO", company_id=88)])
    lead = Lead()
    result = _stage(zi_settings, fake).enrich_company(_fc(make_job, zi_settings, now), lead, "co-1")
    assert result.status == "DOMAIN_MISMATCH"
    assert "search/contact" not in fake.paths() and lead.contacts == []


def test_an_ambiguous_name_match_is_not_attached(zi_settings, make_job, now):
    fc = _fc(make_job, zi_settings, now, company_website=None)
    twins = [{**ACME, "id": 1, "website": None, "domainList": []},
             {**ACME, "id": 2, "website": None, "domainList": []}]
    fake = FakeZoomInfo(companies=twins)
    result = _stage(zi_settings, fake).enrich_company(fc, Lead(domain=None), "co-1")
    assert result.status == "AMBIGUOUS" and "search/contact" not in fake.paths()


def test_a_single_same_name_same_state_company_matches_without_a_domain(zi_settings, make_job, now):
    fc = _fc(make_job, zi_settings, now, company_website=None)
    fake = FakeZoomInfo(companies=[{**ACME, "website": None, "domainList": []}],
                        previews=[_preview(1, "Jane", "Doe", "CEO", has_email=False)])
    lead = Lead(domain=None)
    result = _stage(zi_settings, fake).enrich_company(fc, lead, "co-1")
    assert (result.status, result.match_basis) == ("ENRICHED", "NAME_AND_STATE")
    assert [(c["name"], c["origin"]) for c in lead.contacts] == [("Jane Doe", "ZOOMINFO")]


def test_zoominfo_upgrades_a_weak_public_email_for_the_same_person(zi_settings, make_job, now):
    public = _public("Fred A. McManus", "Chief Operating Officer", 2, "fred@randstadusa.com")
    lead = Lead(contacts=[public])
    fake = FakeZoomInfo(companies=[ACME],
                        previews=[_preview(5, "Fred", "McManus", "COO")],
                        people={5: _person(5, "Fred", "McManus", "COO", "fmcmanus@acme-mfg.com")})
    repo = RecordingRepo()
    result = _stage(zi_settings, fake, repo).enrich_company(_fc(make_job, zi_settings, now), lead, "co-1")

    [fred] = lead.contacts
    assert result.contacts_upgraded == 1 and result.contacts_added == 0
    assert fred["email"] == "fmcmanus@acme-mfg.com"
    assert fred["title"] == "Chief Operating Officer", "the public title is kept"
    assert fred["origin"] == "PUBLIC+ZOOMINFO"
    assert fred["replaced_emails"] == [{"email": "fred@randstadusa.com", "email_class": DOMAIN_MISMATCH}]
    assert fred["field_provenance"]["email"]["source"] == "zoominfo"
    assert fred["field_provenance"]["email"]["zoominfo_person_id"] == 5
    assert repo.calls["contact_update"] == [("c-1", {"email": "fmcmanus@acme-mfg.com",
                                                     "profile_url": "https://www.linkedin.com/in/FredMcManus"})]
    assert {e["key"] for e in repo.calls["evidence"]} >= {
        "zoominfo_company_match", "zoominfo_email", "zoominfo_profile_url"}


def test_a_personal_public_email_is_never_replaced_or_re_enriched(zi_settings, make_job, now):
    zi_settings.contacts_target = 3
    public = _public("Jane Doe", "CEO", 1, "jane.doe@acme-mfg.com")
    lead = Lead(contacts=[public])
    fake = FakeZoomInfo(companies=[ACME],
                        previews=[_preview(1, "Jane", "Doe", "CEO"), _preview(2, "Carl", "Ortiz", "COO")],
                        people={1: _person(1, "Jane", "Doe", "CEO", "jdoe@acme-mfg.com"),
                                2: _person(2, "Carl", "Ortiz", "COO", "cortiz@acme-mfg.com")})
    _stage(zi_settings, fake).enrich_company(_fc(make_job, zi_settings, now), lead, "co-1")

    enrich_body = next(b for p, b, _ in fake.requests if p == "enrich/contact")
    assert enrich_body["matchPersonInput"] == [{"personId": 2}], "Jane already has a PERSONAL email"
    jane = next(c for c in lead.contacts if c["name"] == "Jane Doe")
    assert jane["email"] == "jane.doe@acme-mfg.com"
    assert [c["name"] for c in lead.contacts] == ["Jane Doe", "Carl Ortiz"]


def test_new_contacts_are_poc_only_same_company_and_capped(zi_settings, make_job, now):
    zi_settings.zoominfo_max_contact_enrich_per_company = 1
    fake = FakeZoomInfo(companies=[ACME], previews=[
        _preview(1, "Jane", "Doe", "CEO"),
        _preview(2, "Carl", "Ortiz", "COO", score=99),
        _preview(3, "Sam", "Lee", "Software Engineer"),              # not a POC title
        _preview(4, "Ana", "Ruiz", "Plant Manager", company_id=999),  # another company
    ], people={1: _person(1, "Jane", "Doe", "CEO", "jane.doe@acme-mfg.com")})
    lead = Lead()
    result = _stage(zi_settings, fake).enrich_company(_fc(make_job, zi_settings, now), lead, "co-1")

    enrich_body = next(b for p, b, _ in fake.requests if p == "enrich/contact")
    assert enrich_body["matchPersonInput"] == [{"personId": 1}], "P1 before P2, one credit"
    assert [(c["name"], c["title_priority"], c["email"], c["origin"]) for c in lead.contacts] == [
        ("Jane Doe", 1, "jane.doe@acme-mfg.com", "ZOOMINFO"),
        ("Carl Ortiz", 2, None, "ZOOMINFO"),      # a free search preview, not enriched
    ]
    assert result.contacts_added == 2 and result.credits == 2.0  # company + one contact


def test_a_public_contact_ranks_before_an_equal_zoominfo_contact(zi_settings, make_job, now):
    public = _public("Dana Reed", "President", 1, "dreed@acme-mfg.com")
    lead = Lead(contacts=[public])
    fake = FakeZoomInfo(companies=[ACME], previews=[_preview(9, "Omar", "Haddad", "CEO")],
                        people={9: _person(9, "Omar", "Haddad", "CEO", "ohaddad@acme-mfg.com")})
    _stage(zi_settings, fake).enrich_company(_fc(make_job, zi_settings, now), lead, "co-1")
    assert [(c["name"], c["origin"]) for c in lead.contacts] == [
        ("Dana Reed", "PUBLIC"), ("Omar Haddad", "ZOOMINFO")]


def test_every_call_is_logged_with_cost(zi_settings, make_job, now):
    fake = FakeZoomInfo(companies=[ACME], previews=[_preview(1, "Jane", "Doe", "CEO")],
                        people={1: _person(1, "Jane", "Doe", "CEO", "jane.doe@acme-mfg.com")})
    repo = RecordingRepo()
    _stage(zi_settings, fake, repo).enrich_company(_fc(make_job, zi_settings, now), Lead(), "co-1")
    logs = repo.calls["enrichment_log"]
    assert [(l["endpoint"].rsplit("/", 2)[-2:], l["credit_cost"], l["billable"]) for l in logs] == [
        (["enrich", "company"], 1.0, True), (["search", "contact"], 0.0, True),
        (["enrich", "contact"], 1.0, True)]
    [new] = repo.calls["contact"]
    assert new["source_type"] == "ZOOMINFO" and new["raw_payload"]["origin"] == "ZOOMINFO"


def test_recent_zoominfo_data_is_reused_without_calling(zi_settings, make_job, now):
    class Repo(RecordingRepo):
        configured = True

        def select(self, table, columns="*", limit=None, filters=None, order_by=None, **kw):
            if table == "enrichment_logs":
                return [{"created_at": datetime.now(timezone.utc).isoformat(), "status": "SUCCESS"}]
            return [{"id": "c-9", "name": "Jane Doe", "title": "CEO", "title_priority": 1,
                     "email": "jane.doe@acme-mfg.com", "profile_url": None,
                     "raw_payload": {"origin": "ZOOMINFO"}}]

    fake = FakeZoomInfo(companies=[ACME])
    lead = Lead()
    result = ZoomInfoEnrichment(Repo(), zi_settings, provider=_provider(zi_settings, fake)).enrich_company(
        _fc(make_job, zi_settings, now), lead, "co-1")
    assert result.status == "CACHED" and fake.requests == []
    assert [c["name"] for c in lead.contacts] == ["Jane Doe"]


def test_person_key_ignores_middle_initials_and_suffixes():
    assert person_key("Fred A. McManus") == person_key("fred mcmanus") == ("fred", "mcmanus")
    assert person_key("Carl W Kasalek Jr.") == ("carl", "kasalek")
    assert person_key("Cher") is None


# ===========================================================================
# Runner: QUALIFIED only, after public discovery
# ===========================================================================
def test_zoominfo_runs_after_public_discovery_for_qualified_companies_only(
        zi_settings, recording_repo, make_job, now):
    fake = FakeZoomInfo(companies=[ACME], previews=[_preview(1, "Jane", "Doe", "CEO")],
                        people={1: _person(1, "Jane", "Doe", "CEO", "jane.doe@acme-mfg.com")})
    jobs = [make_job(company="Acme Manufacturing", title="Welder", employees="51 to 200",
                     industry="Manufacturing", company_website="https://acme-mfg.com",
                     description="Apply at apply@indeed.com", external_id="a-1"),
            make_job(company="Mystery Mfg", title="Welder", employees=None,
                     industry="Manufacturing", company_website="https://mysterymfg.com",
                     external_id="m-1")]
    runner = PipelineRunner(settings=zi_settings, repo=recording_repo,
                            access=StubAccess({}, settings=zi_settings),
                            zoominfo=_provider(zi_settings, fake))
    report = runner.run(raw_jobs=jobs, now=now)

    stages = list(report.stages)
    assert stages.index("zoominfo_enrichment") == stages.index("contact_discovery") + 1
    company_inputs = [b["matchCompanyInput"][0] for p, b, _ in fake.requests if p == "enrich/company"]
    assert company_inputs == [{"companyWebsite": "http://www.acme-mfg.com",
                               "companyName": "Acme Manufacturing"}], "NEEDS_REVIEW is never sent"
    [acme] = report.qualified
    [review] = report.needs_review
    assert review.enrichment == {}
    assert acme.enrichment["ZOOMINFO"]["status"] == "ENRICHED"
    emails = {o["email"]: o for o in acme.observed_emails}
    assert emails["jane.doe@acme-mfg.com"]["email_class"] == PERSONAL
    assert emails["jane.doe@acme-mfg.com"]["source"] == "zoominfo"
    assert emails["jane.doe@acme-mfg.com"]["extraction_method"] == "zoominfo_enrich_contact"
    assert emails["apply@indeed.com"]["email_class"] == PORTAL_GENERATED, "preserved as evidence"
    assert acme.emails == ["jane.doe@acme-mfg.com"]
    assert report.zoominfo_enrichment["statuses"] == {"ENRICHED": 1}


def test_stop_at_before_enrichment_runs_public_discovery_only(zi_settings, recording_repo, make_job, now):
    fake = FakeZoomInfo(companies=[ACME])
    runner = PipelineRunner(settings=zi_settings, repo=recording_repo,
                            access=StubAccess({}, settings=zi_settings),
                            zoominfo=_provider(zi_settings, fake))
    report = runner.run(raw_jobs=[make_job(company="Acme Manufacturing", employees="51 to 200",
                                           industry="Manufacturing",
                                           company_website="https://acme-mfg.com")],
                        now=now, stop_at="before_enrichment")
    assert "contact_discovery" in report.stages and "zoominfo_enrichment" not in report.stages
    assert fake.requests == []
