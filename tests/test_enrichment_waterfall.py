"""Enrichment waterfall: ZoomInfo PRIMARY, Apollo and Apify FALLBACK only.

Apollo responses are fixtures in the shapes Apollo's published OpenAPI
definitions give (docs.apollo.io): ``/mixed_people/api_search`` -> ``people[]``
without emails; ``/people/match`` -> ``person{... organization{primary_domain}}``.
No Apollo or Apify account was used; no Apify enrichment actor is confirmed, so
the Apify adapter is exercised with a fake actor registered only in these tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest
import tenacity

from nexbase.enrichment.apify import ApifyAdapter, ApifyEnrichmentActor
from nexbase.enrichment.apollo import ApolloAdapter, ApolloProvider
from nexbase.enrichment.base import (
    PROVIDER_ERROR,
    PROVIDER_NO_MATCH,
    PROVIDER_PARTIAL,
    PROVIDER_RATE_LIMITED,
    PROVIDER_SUCCESS,
    PROVIDER_TIMEOUT,
    PROVIDER_UNAVAILABLE,
    CompanyContext,
    FallbackProvider,
    ProviderCall,
    ProviderContact,
    ProviderOutcome,
)
from nexbase.enrichment.waterfall import EnrichmentWaterfall
from nexbase.enrichment.zoominfo_stage import ZoomInfoAdapter
from nexbase.pipeline.runner import PipelineRunner
from tests.conftest import RecordingRepo
from tests.test_pipeline_e2e import StubAccess
from tests.test_zoominfo import ACME, FakeZoomInfo, _person, _preview, _provider

DOMAIN = "acme-mfg.com"


@dataclass
class Lead:
    company_name: str = "acme manufacturing"
    display_name: str = "Acme Manufacturing"
    domain: str | None = DOMAIN
    contacts: list = field(default_factory=list)
    observed_emails: list = field(default_factory=list)


def _company(domain=DOMAIN):
    return CompanyContext(name="Acme Manufacturing", name_normalized="acme manufacturing",
                          domain=domain, states=frozenset({"OH"}))


def _public(name, title, priority, email=None, cid="c-1"):
    return {"id": cid, "name": name, "title": title, "title_priority": priority, "email": email,
            "profile_url": None, "rank_score": 80.0, "discovery_stage": "PUBLIC_WEB",
            "source_type": "COMPANY_WEBSITE", "source": DOMAIN,
            "source_url": f"https://{DOMAIN}/leadership", "extraction": "text", "origin": "PUBLIC"}


# ===========================================================================
# Stub providers: the waterfall's decisions, one scenario at a time
# ===========================================================================
class Stub(FallbackProvider):
    def __init__(self, name, *outcomes, configured=True, companies=10, lookups=3):
        self.name = name
        self.outcomes = list(outcomes)
        self.configured = configured
        self.max_companies_per_run = companies
        self.max_person_lookups_per_company = lookups
        self.refresh_days = None
        self.asked = []

    def is_configured(self):
        return self.configured

    def find(self, company, need, existing):
        self.asked.append(need)
        return self.outcomes.pop(0)


def _out(provider, status, *contacts, reason=None, credits=1.0, http="SUCCESS"):
    return ProviderOutcome(provider, status, reason=reason, match_method="DOMAIN",
                           provider_company_id="X1", contacts=list(contacts),
                           calls=[ProviderCall(endpoint=f"https://{provider.lower()}.test/x",
                                               payload={}, status=http, billable=http == "SUCCESS",
                                               credit_cost=credits)])


def _person_(name, title, email=None, pid="p1"):
    return ProviderContact(name=name, title=title, email=email, person_id=pid,
                           extraction_method="stub", evidence_url="https://provider.test/person")


def _run(providers, lead=None, settings=None, repo=None, company=None):
    lead = lead or Lead()
    waterfall = EnrichmentWaterfall(providers, repo if repo is not None else RecordingRepo(), settings)
    return waterfall, waterfall.enrich_company(company or _company(), lead, "co-1"), lead


def _statuses(result):
    return [(a["provider"], a["status"]) for a in result.attempts]


def test_a_complete_zoominfo_result_stops_the_waterfall(settings):
    """ZoomInfo finds the CEO with a company-domain personal email: Apollo and Apify are not called."""
    zoominfo = Stub("ZOOMINFO", _out("ZOOMINFO", PROVIDER_SUCCESS,
                                     _person_("Jane Doe", "CEO", "jane.doe@acme-mfg.com")))
    apollo, apify = Stub("APOLLO"), Stub("APIFY")
    _, result, lead = _run([zoominfo, apollo, apify], settings=settings)

    assert _statuses(result) == [("ZOOMINFO", PROVIDER_SUCCESS), ("APOLLO", "NOT_CALLED"),
                                 ("APIFY", "NOT_CALLED")]
    assert result.attempts[1]["reason"] == "ZOOMINFO PROVIDER_SUCCESS"
    assert apollo.asked == [] and apify.asked == []
    assert result.providers_called == ["ZOOMINFO"]


def test_a_ceo_without_an_email_falls_back_to_apollo_for_that_email_only(settings):
    zoominfo = Stub("ZOOMINFO", _out("ZOOMINFO", PROVIDER_SUCCESS, _person_("Jane Doe", "CEO")))
    apollo = Stub("APOLLO", _out("APOLLO", PROVIDER_SUCCESS,
                                 _person_("Jane Doe", "CEO", "jane.doe@acme-mfg.com", pid="a1")))
    apify = Stub("APIFY")
    _, result, lead = _run([zoominfo, apollo, apify], settings=settings)

    assert _statuses(result) == [("ZOOMINFO", PROVIDER_PARTIAL), ("APOLLO", PROVIDER_SUCCESS),
                                 ("APIFY", "NOT_CALLED")]
    assert result.attempts[0]["reason"] == "EMAIL_MISSING"
    assert result.attempts[1]["fallback_reason"] == "ZOOMINFO PROVIDER_PARTIAL: no PERSONAL email for Jane Doe"
    [need] = apollo.asked
    assert need.full is False and [p["name"] for p in need.people] == ["Jane Doe"]
    [jane] = lead.contacts
    assert (jane["email"], jane["origin"]) == ("jane.doe@acme-mfg.com", "ZOOMINFO+APOLLO")
    assert jane["field_provenance"]["email"]["provider"] == "APOLLO"
    assert jane["field_provenance"]["title"]["provider"] == "ZOOMINFO"
    assert apify.asked == []


@pytest.mark.parametrize("status,reason", [
    (PROVIDER_NO_MATCH, "NO_MATCH"), (PROVIDER_NO_MATCH, "AMBIGUOUS"),
    (PROVIDER_ERROR, "HTTP 500"), (PROVIDER_TIMEOUT, "ReadTimeout"),
    (PROVIDER_RATE_LIMITED, "HTTP 429"), (PROVIDER_UNAVAILABLE, "NOT_CONFIGURED"),
])
def test_every_zoominfo_failure_is_named_and_falls_back_to_apollo_for_the_whole_task(
        settings, status, reason):
    zoominfo = Stub("ZOOMINFO", _out("ZOOMINFO", status, reason=reason, credits=0.0,
                                     http=status if status != PROVIDER_NO_MATCH else "NO_MATCH"))
    apollo = Stub("APOLLO", _out("APOLLO", PROVIDER_SUCCESS,
                                 _person_("Jane Doe", "CEO", "jane.doe@acme-mfg.com")))
    _, result, _ = _run([zoominfo, apollo, Stub("APIFY")], settings=settings)

    assert _statuses(result)[:2] == [("ZOOMINFO", status), ("APOLLO", PROVIDER_SUCCESS)]
    assert result.attempts[1]["fallback_reason"] == f"ZOOMINFO {status}: {reason}"
    assert apollo.asked[0].full is True


def test_apify_is_called_only_when_apollo_also_could_not_supply(settings):
    zoominfo = Stub("ZOOMINFO", _out("ZOOMINFO", PROVIDER_NO_MATCH, reason="NO_MATCH", credits=0))
    apollo = Stub("APOLLO", _out("APOLLO", PROVIDER_NO_MATCH, reason="IDENTITY_UNRESOLVED", credits=0))
    apify = Stub("APIFY", _out("APIFY", PROVIDER_SUCCESS,
                               _person_("Jane Doe", "CEO", "jane.doe@acme-mfg.com")))
    _, result, lead = _run([zoominfo, apollo, apify], settings=settings)

    assert _statuses(result) == [("ZOOMINFO", PROVIDER_NO_MATCH), ("APOLLO", PROVIDER_NO_MATCH),
                                 ("APIFY", PROVIDER_SUCCESS)]
    assert result.attempts[2]["fallback_reason"] == "APOLLO PROVIDER_NO_MATCH: IDENTITY_UNRESOLVED"
    assert lead.contacts[0]["origin"] == "APIFY"


def test_nothing_is_called_when_public_evidence_already_suffices(settings):
    settings.contacts_target = 1
    lead = Lead(contacts=[_public("Dana Reed", "President", 1, "dreed@acme-mfg.com")])
    providers = [Stub("ZOOMINFO"), Stub("APOLLO"), Stub("APIFY")]
    _, result, _ = _run(providers, lead=lead, settings=settings)
    assert _statuses(result) == [("ZOOMINFO", "NOT_CALLED"), ("APOLLO", "NOT_CALLED"),
                                 ("APIFY", "NOT_CALLED")]
    assert all(p.asked == [] for p in providers)


# ---------------------------------------------------------------------------
# Quality beats provider order
# ---------------------------------------------------------------------------
def test_a_public_personal_email_is_not_replaced_by_a_generic_zoominfo_email(settings):
    lead = Lead(contacts=[_public("Jane Doe", "CEO", 1, "jane.doe@acme-mfg.com")])
    zoominfo = Stub("ZOOMINFO", _out("ZOOMINFO", PROVIDER_SUCCESS,
                                     _person_("Jane Doe", "CEO", "info@acme-mfg.com")))
    _run([zoominfo, Stub("APOLLO"), Stub("APIFY")], lead=lead, settings=settings)
    [jane] = lead.contacts
    assert jane["email"] == "jane.doe@acme-mfg.com" and "replaced_emails" not in jane


def test_a_zoominfo_personal_email_beats_an_apollo_generic_mailbox(settings):
    zoominfo = Stub("ZOOMINFO", _out("ZOOMINFO", PROVIDER_SUCCESS,
                                     _person_("Jane Doe", "CEO", "jane.doe@acme-mfg.com"),
                                     _person_("Carl Ortiz", "COO", pid="p2")))
    apollo = Stub("APOLLO", _out("APOLLO", PROVIDER_SUCCESS,
                                 _person_("Jane Doe", "CEO", "info@acme-mfg.com", pid="a1"),
                                 _person_("Carl Ortiz", "COO", "cortiz@acme-mfg.com", pid="a2")))
    _, result, lead = _run([zoominfo, apollo, Stub("APIFY")], settings=settings)
    emails = {c["name"]: (c["email"], c["origin"]) for c in lead.contacts}
    assert emails["Jane Doe"] == ("jane.doe@acme-mfg.com", "ZOOMINFO+APOLLO")
    assert emails["Carl Ortiz"] == ("cortiz@acme-mfg.com", "ZOOMINFO+APOLLO")


def test_an_apollo_mailbox_is_not_replaced_by_a_weaker_apify_email_and_all_evidence_stays(settings):
    zoominfo = Stub("ZOOMINFO", _out("ZOOMINFO", PROVIDER_SUCCESS, _person_("Carl Ortiz", "COO")))
    apollo = Stub("APOLLO", _out("APOLLO", PROVIDER_SUCCESS,
                                 _person_("Carl Ortiz", "COO", "operations@acme-mfg.com")))
    apify = Stub("APIFY", _out("APIFY", PROVIDER_SUCCESS,
                               _person_("Carl Ortiz", "COO", "carl.ortiz@gmail.com")))
    _, result, lead = _run([zoominfo, apollo, apify], settings=settings)

    assert _statuses(result) == [("ZOOMINFO", PROVIDER_PARTIAL), ("APOLLO", PROVIDER_PARTIAL),
                                 ("APIFY", PROVIDER_PARTIAL)]
    [carl] = lead.contacts
    assert carl["email"] == "operations@acme-mfg.com"
    assert {e["email"] for e in result.provider_emails} == {
        "operations@acme-mfg.com", "carl.ortiz@gmail.com"}, "the weaker one is kept as evidence"


# ---------------------------------------------------------------------------
# Cost control
# ---------------------------------------------------------------------------
def test_a_rate_limited_provider_is_not_called_again_this_run(settings):
    zoominfo = Stub("ZOOMINFO", _out("ZOOMINFO", PROVIDER_RATE_LIMITED, reason="429", credits=0,
                                     http=PROVIDER_RATE_LIMITED))
    apollo = Stub("APOLLO", *[_out("APOLLO", PROVIDER_SUCCESS,
                                   _person_("Jane Doe", "CEO", "jane.doe@acme-mfg.com"))
                              for _ in range(2)])
    waterfall = EnrichmentWaterfall([zoominfo, apollo, Stub("APIFY")], RecordingRepo(), settings)
    waterfall.enrich_company(_company(), Lead(), "co-1")
    second = waterfall.enrich_company(_company(), Lead(), "co-2")

    assert len(zoominfo.asked) == 1
    assert second.attempts[0]["status"] == PROVIDER_UNAVAILABLE
    assert second.attempts[0]["reason"] == "DISABLED_FOR_RUN_AFTER_PROVIDER_RATE_LIMITED"
    assert second.attempts[1]["fallback_reason"].startswith("ZOOMINFO PROVIDER_UNAVAILABLE")


def test_a_spent_company_budget_is_unavailable_and_costs_nothing(settings):
    zoominfo = Stub("ZOOMINFO", _out("ZOOMINFO", PROVIDER_SUCCESS,
                                     _person_("Jane Doe", "CEO", "jane.doe@acme-mfg.com")),
                    companies=1)
    apollo = Stub("APOLLO", _out("APOLLO", PROVIDER_SUCCESS,
                                 _person_("Jane Doe", "CEO", "jane.doe@acme-mfg.com")))
    waterfall = EnrichmentWaterfall([zoominfo, apollo, Stub("APIFY")], RecordingRepo(), settings)
    waterfall.enrich_company(_company(), Lead(), "co-1")
    second = waterfall.enrich_company(_company(), Lead(), "co-2")
    assert (second.attempts[0]["status"], second.attempts[0]["reason"], second.attempts[0]["called"]) == (
        PROVIDER_UNAVAILABLE, "COMPANY_BUDGET_EXHAUSTED", False)
    assert second.attempts[1]["status"] == PROVIDER_SUCCESS


# ---------------------------------------------------------------------------
# Provenance: "why did we call Apollo for this lead?"
# ---------------------------------------------------------------------------
def test_every_attempt_answers_why_and_every_call_is_logged(settings):
    zoominfo = Stub("ZOOMINFO", _out("ZOOMINFO", PROVIDER_SUCCESS, _person_("Jane Doe", "CEO")))
    apollo = Stub("APOLLO", _out("APOLLO", PROVIDER_SUCCESS,
                                 _person_("Jane Doe", "CEO", "jane.doe@acme-mfg.com", pid="a-77")))
    repo = RecordingRepo()
    _, result, lead = _run([zoominfo, apollo, Stub("APIFY")], settings=settings, repo=repo)

    zi, ap, af = result.attempts
    for attempt in (zi, ap):
        for key in ("provider", "status", "match_method", "person_ids", "fields_supplied",
                    "at", "called", "billable", "credits", "requests"):
            assert key in attempt, key
    assert ap["fallback_reason"] and ap["person_ids"] == ["a-77"]
    assert ap["fields_supplied"] == ["Jane Doe:email"]
    assert af == {**af, "status": "NOT_CALLED", "reason": "APOLLO PROVIDER_SUCCESS", "called": False}
    assert [(log["provider"], log["result"]["attempt_status"]) for log in repo.calls["enrichment_log"]] == [
        ("ZOOMINFO", PROVIDER_PARTIAL), ("APOLLO", PROVIDER_SUCCESS)]
    evidence = {(e["key"], e["source_type"]) for e in repo.calls["evidence"]}
    assert ("apollo_email", "APOLLO") in evidence and ("zoominfo_title", "ZOOMINFO") in evidence


# ===========================================================================
# Apollo provider: documented requests
# ===========================================================================
@dataclass
class FakeApollo:
    previews: list = field(default_factory=list)
    people: dict = field(default_factory=dict)
    status: int = 200
    timeout: bool = False
    requests: list = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1/")
        self.requests.append((request.method, path, request.url.params, request.headers.get("x-api-key")))
        if self.timeout:
            raise httpx.ReadTimeout("slow", request=request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "refused"})
        if path == "mixed_people/api_search":
            return httpx.Response(200, json={"total_entries": len(self.previews), "people": self.previews})
        if path == "people/match":
            key = request.url.params.get("id") or request.url.params.get("name")
            return httpx.Response(200, json={"request_id": 1, "person": self.people.get(key)})
        if path == "organizations/enrich":
            return httpx.Response(200, json={"organization": {
                "id": "org-1", "name": "Acme Manufacturing", "primary_domain": DOMAIN,
                "estimated_num_employees": 120, "industry": "manufacturing"}})
        return httpx.Response(404)


def _apollo_person(pid, name, title, email, domain=DOMAIN):
    return {"id": pid, "name": name, "first_name": name.split()[0], "last_name": name.split()[-1],
            "title": title, "email": email, "email_status": "verified",
            "linkedin_url": f"http://www.linkedin.com/in/{pid}", "organization_id": "org-1",
            "match_confidence": "high",
            "organization": {"id": "org-1", "name": "Acme", "primary_domain": domain,
                             "website_url": f"https://www.{domain}"}}


@pytest.fixture
def ap_settings(settings, monkeypatch):
    settings.apollo_api_key = "apollo-key"
    settings.apollo_api_url = "https://api.apollo.io/api/v1"
    monkeypatch.setattr(ApolloProvider._request.retry, "wait", tenacity.wait_none())
    return settings


def _apollo(settings, fake):
    return ApolloAdapter(settings, api=ApolloProvider(settings, transport=httpx.MockTransport(fake.handler)))


def test_apollo_sends_documented_requests_and_charges_only_for_found_data(ap_settings):
    fake = FakeApollo(previews=[{"id": "p1", "first_name": "Jane", "last_name_obfuscated": "D*e",
                                 "title": "CEO", "has_email": True, "organization": {"name": "Acme"}}],
                      people={"p1": _apollo_person("p1", "Jane Doe", "CEO", "jane.doe@acme-mfg.com")})
    adapter = _apollo(ap_settings, fake)
    outcome = adapter.find(_company(), _need(full=True), [])

    (m1, p1, q1, key1), (m2, p2, q2, _) = fake.requests
    assert (m1, p1, key1) == ("POST", "mixed_people/api_search", "apollo-key")
    assert q1.get_list("q_organization_domains_list[]") == [DOMAIN]
    assert "CEO" in q1.get_list("person_titles[]") and q1.get("include_similar_titles") == "false"
    assert (m2, p2, q2.get("id")) == ("POST", "people/match", "p1")
    assert outcome.status == PROVIDER_SUCCESS
    assert outcome.match_method == "PERSON_ORGANIZATION_DOMAIN"
    assert [(c.name, c.email, c.person_id) for c in outcome.contacts] == [
        ("Jane Doe", "jane.doe@acme-mfg.com", "p1")]
    assert [c.credit_cost for c in outcome.calls] == [0.0, 1.0]


def test_apollo_organization_enrichment_is_a_get_on_the_domain(ap_settings):
    fake = FakeApollo()
    api = ApolloProvider(ap_settings, transport=httpx.MockTransport(fake.handler))
    result = api.enrich("Acme", "www.acme-mfg.com", [])
    [(method, path, params, _)] = fake.requests
    assert (method, path, params.get("domain")) == ("GET", "organizations/enrich", DOMAIN)
    assert result.ok and result.data.employee_count == 120 and result.credit_cost == 1.0


def _need(full=False, people=(), limit=3):
    from nexbase.enrichment.base import EnrichmentNeed

    return EnrichmentNeed(full=full, people=list(people), limit=limit)


def test_apollo_attaches_no_one_whose_organization_is_another_domain(ap_settings):
    fake = FakeApollo(people={"Jane Doe": _apollo_person("p1", "Jane Doe", "CEO",
                                                        "jane@othercorp.com", domain="othercorp.com")})
    outcome = _apollo(ap_settings, fake).find(_company(), _need(people=[{"name": "Jane Doe"}]), [])
    assert (outcome.status, outcome.reason, outcome.contacts) == (
        PROVIDER_NO_MATCH, "IDENTITY_UNRESOLVED", [])


def test_apollo_never_runs_without_a_domain(ap_settings):
    fake = FakeApollo()
    outcome = _apollo(ap_settings, fake).find(_company(domain=None), _need(full=True), [])
    assert (outcome.status, outcome.reason) == (PROVIDER_NO_MATCH, "NO_DOMAIN_FOR_IDENTITY")
    assert fake.requests == []


def test_apollo_skips_previews_of_people_who_already_have_a_personal_email(ap_settings):
    fake = FakeApollo(previews=[{"id": "p1", "first_name": "Jane", "last_name_obfuscated": "D*e",
                                 "title": "CEO", "has_email": True}])
    existing = [_public("Jane Doe", "CEO", 1, "jane.doe@acme-mfg.com")]
    _apollo(ap_settings, fake).find(_company(), _need(full=True), existing)
    assert [path for _, path, _, _ in fake.requests] == ["mixed_people/api_search"]


@pytest.mark.parametrize("fake,expected", [
    (FakeApollo(status=429), PROVIDER_RATE_LIMITED),
    (FakeApollo(status=401), PROVIDER_UNAVAILABLE),
    (FakeApollo(status=403), PROVIDER_UNAVAILABLE),
    (FakeApollo(status=500), PROVIDER_ERROR),
    (FakeApollo(timeout=True), PROVIDER_TIMEOUT),
])
def test_apollo_failures_are_classified(ap_settings, fake, expected):
    outcome = _apollo(ap_settings, fake).find(_company(), _need(full=True), [])
    assert outcome.status == expected
    assert all(not c.billable for c in outcome.calls)


def test_apollo_person_lookups_are_capped_per_company(ap_settings):
    previews = [{"id": f"p{n}", "first_name": f"P{n}", "last_name_obfuscated": "X*x",
                 "title": "Operations Manager", "has_email": True} for n in range(5)]
    fake = FakeApollo(previews=previews)
    _apollo(ap_settings, fake).find(_company(), _need(full=True, limit=2), [])
    assert [path for _, path, _, _ in fake.requests].count("people/match") == 2


# ===========================================================================
# Apify adapter: no confirmed actor, so unavailable unless one is registered
# ===========================================================================
def test_apify_without_a_confirmed_actor_is_unavailable(settings):
    settings.apify_api_token = "token"
    settings.apify_enrichment_actor = "someone~contact-finder"
    outcome = ApifyAdapter(settings).find(_company(), _need(full=True), [])
    assert (outcome.status, outcome.reason, outcome.calls) == (
        PROVIDER_UNAVAILABLE, "NO_CONFIRMED_ACTOR", [])


def _fake_actor():
    return ApifyEnrichmentActor(
        actor_id="test~actor",
        build_input=lambda company, need: {"domain": company.domain},
        parse_item=lambda item: (ProviderContact(
            name=item["name"], title=item["title"], email=item["email"],
            extraction_method="apify_test_actor", evidence_url=item["url"]), item["company_domain"]))


def test_apify_keeps_only_people_it_ties_to_this_employer_and_records_the_cap(settings):
    settings.apify_api_token = "token"
    settings.apify_enrichment_actor = "test~actor"
    seen = {}

    def run(actor_id, actor_input, **kw):
        seen.update(actor_id=actor_id, input=actor_input, **kw)
        return [{"name": "Jane Doe", "title": "CEO", "email": "jane.doe@acme-mfg.com",
                 "url": "https://acme-mfg.com/team", "company_domain": "www.acme-mfg.com"},
                {"name": "Omar Haddad", "title": "CEO", "email": "omar@other.com",
                 "url": "https://other.com/team", "company_domain": "other.com"}]

    adapter = ApifyAdapter(settings, actors={"test~actor": _fake_actor()}, run=run)
    outcome = adapter.find(_company(), _need(full=True, limit=10), [])
    assert [c.name for c in outcome.contacts] == ["Jane Doe"]
    assert seen["input"] == {"domain": DOMAIN} and seen["max_items"] == 10
    [call] = outcome.calls
    assert call.payload["max_total_charge_usd"] == settings.apify_max_charge_usd_per_call
    assert call.credit_cost == settings.apify_max_charge_usd_per_call and call.billable


def test_apify_run_failures_are_classified(settings):
    settings.apify_api_token = "token"
    settings.apify_enrichment_actor = "test~actor"
    request = httpx.Request("POST", "https://api.apify.com/v2/acts/test~actor")

    def run(*a, **kw):
        raise httpx.HTTPStatusError("rate", request=request, response=httpx.Response(429, request=request))

    outcome = ApifyAdapter(settings, actors={"test~actor": _fake_actor()}, run=run).find(
        _company(), _need(full=True), [])
    assert outcome.status == PROVIDER_RATE_LIMITED and outcome.calls[0].credit_cost == 0.0


# ===========================================================================
# Real adapters end to end, and the runner
# ===========================================================================
def test_zoominfo_partial_then_apollo_email_through_the_real_adapters(settings, monkeypatch):
    settings.zoominfo_api_key, settings.zoominfo_api_url = "u:p", "https://api.zoominfo.com"
    settings.apollo_api_key, settings.apollo_api_url = "k", "https://api.apollo.io/api/v1"
    monkeypatch.setattr(ApolloProvider._request.retry, "wait", tenacity.wait_none())
    zi = FakeZoomInfo(companies=[ACME], previews=[_preview(1, "Jane", "Doe", "CEO", has_email=False)])
    ap = FakeApollo(people={"Jane Doe": _apollo_person("a1", "Jane Doe", "Chief Executive Officer",
                                                      "jane.doe@acme-mfg.com")})
    providers = [ZoomInfoAdapter(settings, api=_provider(settings, zi)), _apollo(settings, ap),
                 ApifyAdapter(settings)]
    _, result, lead = _run(providers, settings=settings)

    assert _statuses(result) == [("ZOOMINFO", PROVIDER_PARTIAL), ("APOLLO", PROVIDER_SUCCESS),
                                 ("APIFY", "NOT_CALLED")]
    assert [(m, p, q.get("name"), q.get("domain")) for m, p, q, _ in ap.requests] == [
        ("POST", "people/match", "Jane Doe", DOMAIN)], "only Jane's email was asked for"
    [jane] = lead.contacts
    assert (jane["title"], jane["email"], jane["origin"]) == (
        "CEO", "jane.doe@acme-mfg.com", "ZOOMINFO+APOLLO")


def test_the_runner_records_the_whole_waterfall_on_the_lead(settings, recording_repo, make_job, now):
    job = make_job(company="Acme Manufacturing", employees="51 to 200", industry="Manufacturing",
                   company_website="https://acme-mfg.com")
    report = PipelineRunner(settings=settings, repo=recording_repo,
                            access=StubAccess({}, settings=settings)).run(raw_jobs=[job], now=now)
    [lead] = report.qualified
    assert [(a["provider"], a["status"], a.get("reason")) for a in lead.enrichment["attempts"]] == [
        ("ZOOMINFO", PROVIDER_UNAVAILABLE, "NOT_CONFIGURED"),
        ("APOLLO", PROVIDER_UNAVAILABLE, "NOT_CONFIGURED"),
        ("APIFY", PROVIDER_UNAVAILABLE, "NOT_CONFIGURED"),
    ]
    assert lead.enrichment["attempts"][1]["fallback_reason"] == "ZOOMINFO PROVIDER_UNAVAILABLE: NOT_CONFIGURED"
    assert lead.enrichment["providers_called"] == []
    assert "enrichment_log" not in recording_repo.calls, "nothing was called, nothing is billed"
    assert report.enrichment["statuses"]["APOLLO"] == {PROVIDER_UNAVAILABLE: 1}
