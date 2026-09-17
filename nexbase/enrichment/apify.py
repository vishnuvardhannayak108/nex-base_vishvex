"""Apify: the LAST fallback provider of the enrichment waterfall (paid).

Apify runs third-party actors; each actor has its own input and output schema.
No contact-enrichment actor has been confirmed, so ``APIFY_ENRICHMENT_ACTORS``
is empty and this provider reports PROVIDER_UNAVAILABLE (NO_CONFIRMED_ACTOR).
Adding one means reviewing that actor's documented schema and registering how
to build its input and read its dataset items; ``APIFY_ENRICHMENT_ACTOR`` then
selects it.

Runs use the Apify run API already used for job discovery
(``run-sync-get-dataset-items``, token in the Authorization header) and are
capped by items and by ``maxTotalChargeUsd``. The recorded ``credit_cost`` is
that dollar cap - an upper bound in USD, not provider credits.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from nexbase.config import Settings, get_settings
from nexbase.discovery.apify_sources import APIFY_API, run_actor
from nexbase.enrichment.base import (
    PROVIDER_NO_MATCH,
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


@dataclass(frozen=True)
class ApifyEnrichmentActor:
    actor_id: str
    #: (company, need) -> actor input
    build_input: Callable[[CompanyContext, EnrichmentNeed], dict]
    #: dataset item -> (contact, the employer domain the actor says it belongs to), or None
    parse_item: Callable[[dict], tuple[ProviderContact, str | None] | None]


#: Confirmed contact-enrichment actors, by actor id. Empty until one is reviewed.
APIFY_ENRICHMENT_ACTORS: dict[str, ApifyEnrichmentActor] = {}


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

    @property
    def actor(self) -> ApifyEnrichmentActor | None:
        return self.actors.get(self.settings.apify_enrichment_actor)

    def is_configured(self) -> bool:
        return bool(self.settings.apify_api_token and self.actor)

    def find(self, company: CompanyContext, need: EnrichmentNeed,
             existing: list[dict]) -> ProviderOutcome:
        if not self.settings.apify_api_token:
            return ProviderOutcome(self.name, PROVIDER_UNAVAILABLE, "NOT_CONFIGURED")
        actor = self.actor
        if actor is None:
            return ProviderOutcome(self.name, PROVIDER_UNAVAILABLE, "NO_CONFIRMED_ACTOR")
        if not company.domain:
            return ProviderOutcome(self.name, PROVIDER_NO_MATCH, "NO_DOMAIN_FOR_IDENTITY")

        actor_input = actor.build_input(company, need)
        cap_usd = self.settings.apify_max_charge_usd_per_call
        call = ProviderCall(
            endpoint=f"{APIFY_API}/acts/{actor.actor_id}/run-sync-get-dataset-items",
            payload={"input": actor_input, "max_items": need.limit,
                     "max_total_charge_usd": cap_usd},
            status="SUCCESS", credit_cost=cap_usd)
        outcome = ProviderOutcome(self.name, PROVIDER_SUCCESS, calls=[call],
                                  match_method="CONTACT_COMPANY_DOMAIN")
        try:
            items = self._run(actor.actor_id, actor_input, token=self.settings.apify_api_token,
                              max_items=need.limit, max_charge_usd=cap_usd,
                              timeout_seconds=self.settings.apify_timeout_seconds)
        except Exception as exc:
            call.status, call.error, call.credit_cost = failure_status(exc), str(exc), 0.0
            outcome.status, outcome.reason = call.status, call.error
            return outcome
        call.billable, call.records = True, len(items)

        root = domain_root(company.domain)
        wanted = {person_key(p.get("name")) for p in need.people}
        dropped = 0
        for item in items:
            parsed = actor.parse_item(item) if isinstance(item, dict) else None
            if parsed is None:
                continue
            contact, employer_domain = parsed
            if domain_root(employer_domain) != root:
                dropped += 1  # the actor cannot tie this person to this employer
                continue
            if not need.full and person_key(contact.name) not in wanted:
                continue
            outcome.contacts.append(contact)
        if not outcome.contacts:
            outcome.status = PROVIDER_NO_MATCH
            outcome.reason = "IDENTITY_UNRESOLVED" if dropped else "NO_PERSON_MATCH"
        return outcome
