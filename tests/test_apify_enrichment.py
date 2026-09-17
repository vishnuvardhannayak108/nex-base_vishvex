"""Apify enrichment infrastructure: per-job actors, LinkedIn identity, benchmark.

No Apify actor is selected or registered. Every actor here is a fake defined in
this file; every run is a fake function. No network call is made.
"""
from __future__ import annotations

import httpx
import pytest

from nexbase.config import Settings
from nexbase.enrichment import apify as apify_module
from nexbase.enrichment.apify import (
    APIFY_ENRICHMENT_ACTORS,
    COMPANY_TO_POCS,
    CONFIRMED,
    LINKEDIN_RISK,
    PROFILE_TO_EMAIL,
    WEBSITE_TO_EMAILS,
    ApifyAdapter,
    ApifyEnrichmentActor,
    ApifyItem,
    configured_actor_ids,
    linkedin_company_identity,
)
from nexbase.enrichment.apify_benchmark import (
    BenchmarkCompany,
    benchmark_actor,
    load_companies,
)
from nexbase.enrichment.base import (
    PROVIDER_ERROR,
    PROVIDER_NO_MATCH,
    PROVIDER_RATE_LIMITED,
    PROVIDER_SUCCESS,
    PROVIDER_UNAVAILABLE,
    CompanyContext,
    EnrichmentNeed,
    ProviderContact,
)

DOMAIN = "acme-mfg.com"
ACME_LI = "https://www.linkedin.com/company/acme-mfg"


def _item(name="Jane Doe", title="CEO", email=None, website="https://www.acme-mfg.com",
          company_url=ACME_LI, profile="https://www.linkedin.com/in/janedoe"):
    return ApifyItem(contact=ProviderContact(name=name, title=title, email=email, profile_url=profile,
                                             extraction_method="apify:test", evidence_url=profile),
                     company_linkedin_url=company_url, company_website=website, profile_url=profile)


# ===========================================================================
# LinkedIn company <-> employer domain
# ===========================================================================
@pytest.mark.parametrize("website", [
    "https://www.acme-mfg.com", "http://acme-mfg.com", "acme-mfg.com", "www.acme-mfg.com/",
    "HTTPS://WWW.ACME-MFG.COM/about-us?ref=li",
])
def test_the_linkedin_website_matches_after_www_protocol_and_path_normalization(website):
    result = linkedin_company_identity(DOMAIN, [_item(website=website)])
    assert result.status == CONFIRMED and [i.contact.name for i in result.confirmed] == ["Jane Doe"]


def test_a_subdomain_of_the_employer_domain_confirms():
    assert linkedin_company_identity("www.acme-mfg.com", [
        _item(website="https://careers.acme-mfg.com")]).status == CONFIRMED


@pytest.mark.parametrize("website", [
    "https://acme-holdings.com",
    "https://acme-mfg.com.lookalike.io",      # the employer domain is only a prefix
    "https://acme-mfg.co",
])
def test_a_different_domain_is_a_mismatch(website):
    result = linkedin_company_identity(DOMAIN, [_item(website=website)])
    assert (result.status, result.confirmed) == ("DOMAIN_MISMATCH", [])


@pytest.mark.parametrize("website", [None, "", "https://www.linkedin.com/company/acme-mfg",
                                     "https://facebook.com/acmemfg"])
def test_a_missing_or_platform_website_confirms_nothing(website):
    result = linkedin_company_identity(DOMAIN, [_item(website=website)])
    assert (result.status, result.confirmed) == ("LINKEDIN_WEBSITE_MISSING", [])


def test_two_linkedin_companies_on_the_domain_are_ambiguous():
    result = linkedin_company_identity(DOMAIN, [
        _item(name="Jane Doe", company_url=ACME_LI),
        _item(name="Omar Haddad", company_url="https://www.linkedin.com/company/acme-mfg-holdings"),
    ])
    assert (result.status, result.confirmed) == ("AMBIGUOUS", [])


def test_one_linkedin_company_disagreeing_about_its_website_is_ambiguous():
    result = linkedin_company_identity(DOMAIN, [
        _item(name="Jane Doe"), _item(name="Omar Haddad", website="https://acme-holdings.com")])
    assert result.status == "AMBIGUOUS"


def test_only_the_company_on_the_domain_attaches_when_another_is_off_domain():
    result = linkedin_company_identity(DOMAIN, [
        _item(name="Jane Doe"),
        _item(name="Omar Haddad", company_url="https://www.linkedin.com/company/other",
              website="https://other.com")])
    assert result.status == CONFIRMED
    assert [i.contact.name for i in result.confirmed] == ["Jane Doe"]


def test_a_company_name_alone_never_matches():
    item = _item(website=None, company_url=None)
    item.contact.raw["company_name"] = "Acme Manufacturing"
    assert linkedin_company_identity(DOMAIN, [item]).status == "NO_COMPANY"
    assert linkedin_company_identity(None, [_item()]).status == "NO_COMPANY"


# ===========================================================================
# Per-job configuration and selection
# ===========================================================================
def test_every_job_defaults_to_no_actor_and_nothing_is_registered():
    fresh = Settings(_env_file=None)
    assert APIFY_ENRICHMENT_ACTORS == {}
    for job in (COMPANY_TO_POCS, PROFILE_TO_EMAIL, WEBSITE_TO_EMAILS):
        assert configured_actor_ids(fresh, job) == []
    assert not hasattr(fresh, "apify_enrichment_actor")


def _actor(actor_id, job, items=None, scrapes_linkedin=False):
    return ApifyEnrichmentActor(actor_id=actor_id, job=job,
                                build_input=lambda company, need: {"domain": company.domain},
                                parse_item=lambda raw: raw["parsed"],
                                scrapes_linkedin=scrapes_linkedin)


class FakeRun:
    def __init__(self, results):
        #: actor id -> list of dataset items, or an exception to raise
        self.results = results
        self.calls = []

    def __call__(self, actor_id, actor_input, **kwargs):
        self.calls.append(actor_id)
        result = self.results.get(actor_id, [])
        if isinstance(result, Exception):
            raise result
        return [{"parsed": item} for item in result]


def _company():
    return CompanyContext(name="Acme Manufacturing", name_normalized="acme manufacturing",
                          domain=DOMAIN, states=frozenset({"OH"}))


@pytest.fixture
def apify_settings(settings):
    settings.apify_api_token = "token-for-mocks"
    return settings


def _need(full=True, people=(), limit=10):
    return EnrichmentNeed(full=full, people=list(people), limit=limit)


def test_default_configuration_is_unavailable_without_any_call(apify_settings):
    run = FakeRun({})
    outcome = ApifyAdapter(apify_settings, run=run).find(_company(), _need(), [])
    assert (outcome.status, outcome.reason) == (PROVIDER_UNAVAILABLE, "NO_CONFIRMED_ACTOR:company_to_pocs")
    assert run.calls == [] and not ApifyAdapter(apify_settings, run=run).is_configured()


def test_finding_pocs_runs_the_company_to_pocs_actor_only(apify_settings):
    apify_settings.apify_company_to_pocs_actor = "fake~pocs"
    apify_settings.apify_profile_to_email_actor = "fake~profile"
    actors = {"fake~pocs": _actor("fake~pocs", COMPANY_TO_POCS),
              "fake~profile": _actor("fake~profile", PROFILE_TO_EMAIL)}
    run = FakeRun({"fake~pocs": [_item()]})
    outcome = ApifyAdapter(apify_settings, actors=actors, run=run).find(_company(), _need(), [])
    assert run.calls == ["fake~pocs"]
    assert (outcome.status, outcome.match_method) == (PROVIDER_SUCCESS, "LINKEDIN_COMPANY_WEBSITE")
    assert outcome.contacts[0].raw["company_linkedin_url"] == ACME_LI


def test_filling_emails_runs_the_profile_to_email_actor_for_people_with_profiles(apify_settings):
    apify_settings.apify_company_to_pocs_actor = "fake~pocs"
    apify_settings.apify_profile_to_email_actor = "fake~profile"
    actors = {"fake~pocs": _actor("fake~pocs", COMPANY_TO_POCS),
              "fake~profile": _actor("fake~profile", PROFILE_TO_EMAIL)}
    run = FakeRun({"fake~profile": [_item(email="jane.doe@acme-mfg.com")]})
    people = [{"name": "Jane Doe", "profile_url": "https://linkedin.com/in/janedoe/"}]
    outcome = ApifyAdapter(apify_settings, actors=actors, run=run).find(
        _company(), _need(full=False, people=people), [])
    assert run.calls == ["fake~profile"]
    assert [c.email for c in outcome.contacts] == ["jane.doe@acme-mfg.com"]


def test_a_profile_result_for_someone_not_asked_about_or_at_another_employer_is_dropped(apify_settings):
    apify_settings.apify_profile_to_email_actor = "fake~profile"
    actors = {"fake~profile": _actor("fake~profile", PROFILE_TO_EMAIL)}
    run = FakeRun({"fake~profile": [
        _item(name="Omar Haddad", profile="https://www.linkedin.com/in/omar", email="omar@acme-mfg.com"),
        _item(website="https://acme-holdings.com", email="jane@acme-holdings.com"),
    ]})
    people = [{"name": "Jane Doe", "profile_url": "https://www.linkedin.com/in/janedoe"}]
    outcome = ApifyAdapter(apify_settings, actors=actors, run=run).find(
        _company(), _need(full=False, people=people), [])
    assert outcome.status == PROVIDER_NO_MATCH and outcome.contacts == []
    assert "DOMAIN_MISMATCH" in outcome.reason and "NOT_ASKED_FOR" in outcome.reason


def test_people_without_a_linkedin_profile_url_cannot_use_profile_to_email(apify_settings):
    apify_settings.apify_profile_to_email_actor = "fake~profile"
    run = FakeRun({})
    outcome = ApifyAdapter(apify_settings, actors={"fake~profile": _actor("fake~profile", PROFILE_TO_EMAIL)},
                           run=run).find(_company(), _need(full=False, people=[{"name": "Jane Doe"}]), [])
    assert (outcome.status, outcome.reason, run.calls) == (PROVIDER_NO_MATCH, "NO_LINKEDIN_PROFILE_URL", [])


def test_an_actor_registered_for_another_job_is_never_used(apify_settings):
    apify_settings.apify_company_to_pocs_actor = "fake~profile"
    run = FakeRun({"fake~profile": [_item()]})
    outcome = ApifyAdapter(apify_settings, actors={"fake~profile": _actor("fake~profile", PROFILE_TO_EMAIL)},
                           run=run).find(_company(), _need(), [])
    assert outcome.status == PROVIDER_UNAVAILABLE and run.calls == []


@pytest.mark.parametrize("primary_result", [[], httpx.ConnectError("down"),
                                            [_item(website="https://other.com")]])
def test_the_backup_actor_runs_when_the_primary_fails_or_attaches_nothing(apify_settings, primary_result):
    apify_settings.apify_company_to_pocs_actor = "fake~primary"
    apify_settings.apify_company_to_pocs_backup_actor = "fake~backup"
    actors = {a: _actor(a, COMPANY_TO_POCS) for a in ("fake~primary", "fake~backup")}
    run = FakeRun({"fake~primary": primary_result, "fake~backup": [_item()]})
    outcome = ApifyAdapter(apify_settings, actors=actors, run=run).find(_company(), _need(), [])
    assert run.calls == ["fake~primary", "fake~backup"]
    assert outcome.status == PROVIDER_SUCCESS and len(outcome.calls) == 2


def test_the_backup_does_not_run_after_a_primary_success_or_a_rate_limit(apify_settings):
    apify_settings.apify_company_to_pocs_actor = "fake~primary"
    apify_settings.apify_company_to_pocs_backup_actor = "fake~backup"
    actors = {a: _actor(a, COMPANY_TO_POCS) for a in ("fake~primary", "fake~backup")}
    run = FakeRun({"fake~primary": [_item()], "fake~backup": [_item()]})
    ApifyAdapter(apify_settings, actors=actors, run=run).find(_company(), _need(), [])
    assert run.calls == ["fake~primary"]

    request = httpx.Request("POST", "https://api.apify.com/v2/acts/fake~primary")
    limited = httpx.HTTPStatusError("429", request=request, response=httpx.Response(429, request=request))
    run = FakeRun({"fake~primary": limited, "fake~backup": [_item()]})
    outcome = ApifyAdapter(apify_settings, actors=actors, run=run).find(_company(), _need(), [])
    assert run.calls == ["fake~primary"] and outcome.status == PROVIDER_RATE_LIMITED


def test_website_to_emails_is_configurable_but_never_run(apify_settings):
    apify_settings.apify_website_to_emails_actor = "fake~site"
    run = FakeRun({"fake~site": [_item()]})
    adapter = ApifyAdapter(apify_settings, actors={"fake~site": _actor("fake~site", WEBSITE_TO_EMAILS)}, run=run)
    outcome = adapter.find(_company(), _need(), [])
    assert run.calls == [] and outcome.status == PROVIDER_UNAVAILABLE and not adapter.is_configured()


def test_a_linkedin_scraping_actor_carries_its_risk_on_every_call(apify_settings):
    apify_settings.apify_company_to_pocs_actor = "fake~pocs"
    actors = {"fake~pocs": _actor("fake~pocs", COMPANY_TO_POCS, scrapes_linkedin=True)}
    outcome = ApifyAdapter(apify_settings, actors=actors, run=FakeRun({"fake~pocs": [_item()]})).find(
        _company(), _need(), [])
    assert outcome.calls[0].payload["risks"] == [LINKEDIN_RISK]


def test_the_registry_is_not_modified_by_adapters(apify_settings):
    ApifyAdapter(apify_settings, actors={"fake~x": _actor("fake~x", COMPANY_TO_POCS)})
    assert apify_module.APIFY_ENRICHMENT_ACTORS == {}


# ===========================================================================
# Benchmark (mocks only; the real benchmark has not been run)
# ===========================================================================
class Clock:
    def __init__(self, step):
        self.now, self.step = 0.0, step

    def __call__(self):
        self.now += self.step
        return self.now


def test_the_benchmark_scores_every_metric_without_network(apify_settings):
    actor = _actor("fake~pocs", COMPANY_TO_POCS, scrapes_linkedin=True)
    companies = [
        BenchmarkCompany("Acme Manufacturing", DOMAIN, "OH"),
        BenchmarkCompany("Other Co", "other.com", "TX"),
        BenchmarkCompany("Down Co", "down.com", "CA"),
    ]
    request = httpx.Request("POST", "https://api.apify.com/v2/acts/fake~pocs")

    def run(actor_id, actor_input, **kwargs):
        domain = actor_input["domain"]
        if domain == DOMAIN:
            items = [_item(name="Jane Doe", title="CEO", email="jane.doe@acme-mfg.com"),
                     _item(name="Jane Doe", title="CEO", email="jane.doe@acme-mfg.com"),
                     _item(name="Carl Ortiz", title="COO", email="info@acme-mfg.com",
                           profile="https://www.linkedin.com/in/carl"),
                     _item(name="Sam Lee", title="Software Engineer", email="sam@gmail.com",
                           profile="https://www.linkedin.com/in/sam")]
            return [{"parsed": i} for i in items], {"usage_total_usd": 0.012}
        if domain == "other.com":
            return [{"parsed": _item(website="https://not-other.com")}]
        raise httpx.HTTPStatusError("500", request=request, response=httpx.Response(500, request=request))

    report = benchmark_actor(actor, companies, apify_settings, max_items=10, run=run, clock=Clock(0.5))

    assert report["risks"] == [LINKEDIN_RISK]
    assert report["poc_coverage"] == round(1 / 3, 3)
    assert (report["items_parsed"], report["contacts_attached"]) == (5, 4)
    assert report["correct_company_rate"] == 0.8
    assert report["title_match_rate"] == 0.75
    assert report["email_classes"] == {"PERSONAL": 2, "ROLE": 1, "EXTERNAL_UNVERIFIED": 1}
    assert report["duplicate_rate"] == 0.25
    assert report["errors"] == {PROVIDER_ERROR: 1}
    assert report["identity_statuses"] == {CONFIRMED: 1, "DOMAIN_MISMATCH": 1}
    assert report["runtime_seconds"]["total"] == 1.5
    assert report["cost"]["actual_usd_total"] == 0.012 and report["cost"]["actual_reported_runs"] == 1
    assert report["cost"]["cap_usd_total"] == 2 * apify_settings.apify_max_charge_usd_per_call


def test_actual_cost_is_none_when_the_run_does_not_report_usage(apify_settings):
    actor = _actor("fake~pocs", COMPANY_TO_POCS)
    report = benchmark_actor(actor, [BenchmarkCompany("Acme", DOMAIN)], apify_settings,
                             run=lambda *a, **k: [{"parsed": _item()}], clock=Clock(0.1))
    assert report["cost"]["actual_usd_total"] is None


def test_the_example_company_fixture_loads(tmp_path):
    companies = load_companies("benchmarks/apify/companies.example.json")
    assert [c.domain for c in companies] == ["example-construction.example", "example-manufacturing.example"]
    assert companies[1].known_people[0]["profile_url"].startswith("https://www.linkedin.com/in/")


def test_the_benchmark_script_refuses_to_run_without_credentials(settings, capsys):
    from scripts.benchmark_apify_actors import PENDING, main

    settings.apify_api_token = ""
    code = main(["--companies", "benchmarks/apify/companies.example.json",
                 "--actors", "benchmarks/apify/actors_template.py"], settings=settings)
    assert code == PENDING
    assert "PENDING CLIENT CREDENTIALS" in capsys.readouterr().err


def test_the_benchmark_script_refuses_without_actors_or_paid_run_confirmation(settings, tmp_path, capsys):
    from scripts.benchmark_apify_actors import PENDING, main

    settings.apify_api_token = "token-for-mocks"
    assert main(["--companies", "benchmarks/apify/companies.example.json",
                 "--actors", "benchmarks/apify/actors_template.py"], settings=settings) == PENDING
    actors = tmp_path / "actors.py"
    actors.write_text("from nexbase.enrichment.apify import ApifyEnrichmentActor\n"
                      "ACTORS = [ApifyEnrichmentActor('fake~x', 'company_to_pocs', lambda c, n: {}, lambda i: None)]\n",
                      encoding="utf-8")
    assert main(["--companies", "benchmarks/apify/companies.example.json",
                 "--actors", str(actors)], settings=settings) == PENDING
    assert "--confirm-paid-runs" in capsys.readouterr().err
