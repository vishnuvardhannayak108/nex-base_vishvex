"""Apify: the LAST fallback provider of the enrichment waterfall (paid).

Apify runs third-party actors, each with its own input and output schema.
Enrichment is split into independent jobs, each configured with a primary and an
optional backup actor:

* ``company_to_pocs``   find POC contacts at a company
  (``APIFY_COMPANY_TO_POCS_ACTOR`` / ``..._BACKUP_ACTOR``);
* ``profile_to_email``  find the email of a known person from their LinkedIn
  profile URL (``APIFY_PROFILE_TO_EMAIL_ACTOR`` / ``..._BACKUP_ACTOR``);
* ``website_to_emails`` public emails from the employer's website
  (``APIFY_WEBSITE_TO_EMAILS_ACTOR`` / ``..._BACKUP_ACTOR``). **Deferred**: Phase 6
  already crawls employer sites; configurable, but never run by the waterfall.

The job is chosen by what the waterfall asks for: "find POCs" runs
``company_to_pocs``; "fill these people's emails" runs ``profile_to_email`` for
the people with a LinkedIn profile URL. The backup actor runs only when the
primary is not registered, fails, or returns nothing usable.

**No actor is selected or registered.** ``APIFY_ENRICHMENT_ACTORS`` is empty and
every actor setting defaults to empty, so this provider reports
PROVIDER_UNAVAILABLE. An actor is registered only after it is confirmed and its
real input/output schema has been validated; live validation is pending client
credentials. Actors that scrape LinkedIn also need client/business approval
before production use (``scrapes_linkedin``).

Identity (``linkedin_company_identity``): an employee is attached only when the
LinkedIn company the actor reports publishes a website on the employer's domain,
and exactly one such LinkedIn company is in the results. A missing website, a
different domain, or two LinkedIn companies on the domain attach nothing. A
company name alone never matches.

Runs use the Apify run API already used for job discovery
(``run-sync-get-dataset-items``, token in the Authorization header), capped by
items and by ``maxTotalChargeUsd``. The recorded ``credit_cost`` is that dollar
cap - an upper bound in USD, not provider credits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

from nexbase.config import Settings, get_settings
from nexbase.discovery.apify_sources import APIFY_API, run_actor
from nexbase.enrichment.base import (
    FAILURE_STATUSES,
    PROVIDER_NO_MATCH,
    PROVIDER_RATE_LIMITED,
    PROVIDER_SUCCESS,
    PROVIDER_UNAVAILABLE,
    CompanyContext,
    EnrichmentNeed,
    FallbackProvider,
    ProviderCall,
    ProviderContact,
    ProviderOutcome,
    failure_status,
)
from nexbase.enrichment.waterfall import domain_root, person_key
from nexbase.pipeline.normalize import NON_EMPLOYER_HOSTS

COMPANY_TO_POCS = "company_to_pocs"
PROFILE_TO_EMAIL = "profile_to_email"
WEBSITE_TO_EMAILS = "website_to_emails"
JOBS = (COMPANY_TO_POCS, PROFILE_TO_EMAIL, WEBSITE_TO_EMAILS)
#: Jobs the waterfall runs. website_to_emails is deferred (see module docstring).
ACTIVE_JOBS = (COMPANY_TO_POCS, PROFILE_TO_EMAIL)

#: Compliance risk recorded for any actor that scrapes LinkedIn.
LINKEDIN_RISK = "LINKEDIN_SCRAPING_REQUIRES_CLIENT_APPROVAL"


@dataclass
class ApifyItem:
    """One dataset item read by an actor definition."""

    contact: ProviderContact
    #: The LinkedIn company the actor says this person works for.
    company_linkedin_url: str | None = None
    #: That LinkedIn company's published website.
    company_website: str | None = None
    #: The person's LinkedIn profile URL (profile_to_email: must be one asked for).
    profile_url: str | None = None


@dataclass(frozen=True)
class ApifyEnrichmentActor:
    actor_id: str
    job: str
    #: (company, need) -> actor input
    build_input: Callable[[CompanyContext, EnrichmentNeed], dict]
    #: dataset item -> ApifyItem, or None when the item is not a person
    parse_item: Callable[[dict], ApifyItem | None]
    scrapes_linkedin: bool = False

    @property
    def risks(self) -> list[str]:
        return [LINKEDIN_RISK] if self.scrapes_linkedin else []


#: Confirmed actors, by actor id. Empty: no actor is selected or registered.
APIFY_ENRICHMENT_ACTORS: dict[str, ApifyEnrichmentActor] = {}


def configured_actor_ids(settings: Settings, job: str) -> list[str]:
    """Primary then backup actor id configured for ``job`` (empty strings dropped)."""
    ids = (getattr(settings, f"apify_{job}_actor", ""),
           getattr(settings, f"apify_{job}_backup_actor", ""))
    return [actor_id for actor_id in dict.fromkeys(ids) if actor_id]


# ---------------------------------------------------------------------------
# LinkedIn company <-> employer domain
# ---------------------------------------------------------------------------
CONFIRMED = "CONFIRMED"


@dataclass
class IdentityResult:
    #: CONFIRMED | DOMAIN_MISMATCH | LINKEDIN_WEBSITE_MISSING | AMBIGUOUS | NO_COMPANY
    status: str
    #: The one LinkedIn company confirmed on the employer's domain.
    company_key: str | None = None
    confirmed: list[ApifyItem] = field(default_factory=list)


def website_root(website: str | None) -> str:
    """The registrable domain a LinkedIn company publishes, or "" when it names none.

    A profile on a platform (linkedin.com, facebook.com, an ATS ...) is not the
    company's own website.
    """
    root = domain_root(website)
    return "" if not root or root in NON_EMPLOYER_HOSTS else root


def _linkedin_company_key(item: ApifyItem) -> str | None:
    url = (item.company_linkedin_url or "").strip()
    if not url:
        return None
    parts = urlsplit(url if "//" in url else f"//{url}")
    return f"{parts.netloc.lower().removeprefix('www.')}{parts.path.rstrip('/').lower()}"


def linkedin_company_identity(employer_domain: str | None,
                              items: list[ApifyItem]) -> IdentityResult:
    """Which results belong to the employer, by the LinkedIn company's website.

    Items are grouped by the LinkedIn company they name (its URL, else its
    website). A group is confirmed only when its published website's registrable
    domain equals the employer's; "www.", scheme, path and subdomains of that
    domain are normalized away. Exactly one confirmed group attaches; two are
    AMBIGUOUS. Items naming no company at all never attach.
    """
    employer = domain_root(employer_domain)
    if not employer:
        return IdentityResult("NO_COMPANY")
    groups: dict[str, list[ApifyItem]] = {}
    for item in items:
        key = _linkedin_company_key(item) or website_root(item.company_website)
        if key:
            groups.setdefault(key, []).append(item)
    if not groups:
        return IdentityResult("NO_COMPANY")

    confirmed = {key: group for key, group in groups.items()
                 if any(website_root(i.company_website) == employer for i in group)}
    if len(confirmed) > 1:
        return IdentityResult("AMBIGUOUS")
    if len(confirmed) == 1:
        key, group = next(iter(confirmed.items()))
        # A group whose items disagree about the website is not confirmed.
        roots = {website_root(i.company_website) for i in group} - {""}
        if roots != {employer}:
            return IdentityResult("AMBIGUOUS")
        return IdentityResult(CONFIRMED, company_key=key, confirmed=group)
    websites = [website_root(i.company_website) for g in groups.values() for i in g]
    return IdentityResult("DOMAIN_MISMATCH" if any(websites) else "LINKEDIN_WEBSITE_MISSING")


def _profile_key(url: str | None) -> str | None:
    if not url or "linkedin.com/in/" not in url.lower():
        return None
    path = urlsplit(url if "//" in url else f"//{url}").path.rstrip("/").lower()
    return path or None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------
class ApifyAdapter(FallbackProvider):
    name = "APIFY"
    refresh_days = None

    def __init__(self, settings: Settings | None = None,
                 actors: dict[str, ApifyEnrichmentActor] | None = None,
                 run: Callable[..., list[dict]] = run_actor) -> None:
        self.settings = settings or get_settings()
        self.actors = APIFY_ENRICHMENT_ACTORS if actors is None else actors
        self._run = run
        self.max_companies_per_run = self.settings.apify_enrichment_max_companies_per_run
        self.max_person_lookups_per_company = self.settings.apify_enrichment_max_items

    def actors_for(self, job: str) -> list[ApifyEnrichmentActor]:
        """Registered actors configured for ``job``: primary first, then backup."""
        return [self.actors[a] for a in configured_actor_ids(self.settings, job)
                if a in self.actors and self.actors[a].job == job]

    def is_configured(self) -> bool:
        return bool(self.settings.apify_api_token
                    and any(self.actors_for(job) for job in ACTIVE_JOBS))

    def find(self, company: CompanyContext, need: EnrichmentNeed,
             existing: list[dict]) -> ProviderOutcome:
        if not self.settings.apify_api_token:
            return ProviderOutcome(self.name, PROVIDER_UNAVAILABLE, "NOT_CONFIGURED")
        if not company.domain:
            return ProviderOutcome(self.name, PROVIDER_NO_MATCH, "NO_DOMAIN_FOR_IDENTITY")
        outcome = ProviderOutcome(self.name, PROVIDER_SUCCESS)
        jobs = []
        if need.full:
            jobs.append(COMPANY_TO_POCS)
        if any(_profile_key(p.get("profile_url")) for p in need.people):
            jobs.append(PROFILE_TO_EMAIL)
        if not jobs:
            outcome.status, outcome.reason = PROVIDER_NO_MATCH, "NO_LINKEDIN_PROFILE_URL"
            return outcome

        reasons: list[str] = []
        for job in jobs:
            reasons.append(self._run_job(job, company, need, outcome))
            if outcome.status in (PROVIDER_RATE_LIMITED, PROVIDER_UNAVAILABLE) and outcome.calls:
                break  # the account is limited or refused: do not start another run
        if not outcome.contacts and outcome.status == PROVIDER_SUCCESS:
            only_unavailable = all(r.startswith("NO_CONFIRMED_ACTOR") for r in reasons)
            outcome.status = PROVIDER_UNAVAILABLE if only_unavailable else PROVIDER_NO_MATCH
        if not outcome.contacts:
            outcome.reason = "; ".join(reasons)
        return outcome

    def _run_job(self, job: str, company: CompanyContext, need: EnrichmentNeed,
                 outcome: ProviderOutcome) -> str:
        actors = self.actors_for(job)
        if not actors:
            return f"NO_CONFIRMED_ACTOR:{job}"
        reason = f"NO_PERSON_MATCH:{job}"
        for actor in actors:  # primary, then backup
            items, call = self.invoke(actor, company, need)
            outcome.calls.append(call)
            if call.status in FAILURE_STATUSES:
                outcome.status, reason = call.status, f"{call.status}:{actor.actor_id}"
                if call.status in (PROVIDER_RATE_LIMITED, PROVIDER_UNAVAILABLE):
                    return reason
                continue
            attached, reason = (self.attach_company_pocs(company, items, actor)
                                if job == COMPANY_TO_POCS
                                else self.attach_profile_emails(company, need, items, actor))
            if attached:
                outcome.status = PROVIDER_SUCCESS
                outcome.match_method = attached[0].raw.get("match_method")
                outcome.contacts.extend(attached)
                return "ATTACHED"
        return reason

    def invoke(self, actor: ApifyEnrichmentActor, company, need) -> tuple[list[ApifyItem], ProviderCall]:
        """Run one actor once; failures are classified on the returned call."""
        actor_input = actor.build_input(company, need)
        cap_usd = self.settings.apify_max_charge_usd_per_call
        call = ProviderCall(
            endpoint=f"{APIFY_API}/acts/{actor.actor_id}/run-sync-get-dataset-items",
            payload={"job": actor.job, "input": actor_input, "max_items": need.limit,
                     "max_total_charge_usd": cap_usd, "risks": actor.risks},
            status="SUCCESS", credit_cost=cap_usd)
        try:
            raw = self._run(actor.actor_id, actor_input, token=self.settings.apify_api_token,
                            max_items=need.limit, max_charge_usd=cap_usd,
                            timeout_seconds=self.settings.apify_timeout_seconds)
        except Exception as exc:
            call.status, call.error, call.credit_cost = failure_status(exc), str(exc), 0.0
            return [], call
        call.billable, call.records = True, len(raw)
        items = [parsed for parsed in (actor.parse_item(i) for i in raw if isinstance(i, dict))
                 if parsed is not None]
        return items, call

    @staticmethod
    def attach_company_pocs(company, items, actor) -> tuple[list[ProviderContact], str]:
        identity = linkedin_company_identity(company.domain, items)
        if identity.status != CONFIRMED:
            return [], f"{identity.status}:{actor.actor_id}"
        for item in identity.confirmed:
            item.contact.raw.update(company_linkedin_url=item.company_linkedin_url,
                                    company_website=item.company_website,
                                    apify_actor=actor.actor_id, match_method="LINKEDIN_COMPANY_WEBSITE")
        return [i.contact for i in identity.confirmed], "ATTACHED"

    @staticmethod
    def attach_profile_emails(company, need, items, actor) -> tuple[list[ProviderContact], str]:
        """A profile result attaches only for a person asked about, at this employer."""
        asked = {_profile_key(p.get("profile_url")): person_key(p.get("name"))
                 for p in need.people if _profile_key(p.get("profile_url"))}
        employer = domain_root(company.domain)
        attached, statuses = [], set()
        for item in items:
            wanted_name = asked.get(_profile_key(item.profile_url or item.contact.profile_url))
            if wanted_name is None or person_key(item.contact.name) != wanted_name:
                statuses.add("NOT_ASKED_FOR")
                continue
            root = website_root(item.company_website)
            if root != employer:
                statuses.add("DOMAIN_MISMATCH" if root else "LINKEDIN_WEBSITE_MISSING")
                continue
            item.contact.raw.update(company_website=item.company_website,
                                    apify_actor=actor.actor_id, match_method="PROFILE_AND_COMPANY_WEBSITE")
            attached.append(item.contact)
        reason = "ATTACHED" if attached else f"{'|'.join(sorted(statuses)) or 'NO_PERSON_MATCH'}:{actor.actor_id}"
        return attached, reason
