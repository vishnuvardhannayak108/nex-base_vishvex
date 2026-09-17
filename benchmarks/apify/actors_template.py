"""Template for benchmark actor definitions. Contains NO actors.

Copy to ``benchmarks/apify/actors.py`` (not committed) and define one
``ApifyEnrichmentActor`` per shortlisted candidate, only after reading that actor's
documented input and output schema on its Apify Store page. Field names must come
from that documentation, never be guessed.

Defining an actor here benchmarks it only. It is NOT registered for the enrichment
waterfall: production registration (``nexbase/enrichment/apify.py``,
``APIFY_ENRICHMENT_ACTORS``) happens only after explicit confirmation, client
credentials, and live validation of its real schema. Set ``scrapes_linkedin=True``
for any actor that scrapes LinkedIn: that is a compliance risk requiring
client/business approval before production use.

Shape of a definition (placeholders, not a real actor):

    from nexbase.enrichment.apify import ApifyEnrichmentActor, ApifyItem, COMPANY_TO_POCS
    from nexbase.enrichment.base import ProviderContact

    def build_input(company, need):
        return {"<documented input field>": company.domain, "<max field>": need.limit}

    def parse_item(item):
        return ApifyItem(
            contact=ProviderContact(
                name=item.get("<documented name field>"),
                title=item.get("<documented title field>"),
                email=item.get("<documented email field>"),
                profile_url=item.get("<documented profile URL field>"),
                extraction_method="apify:<actor id>",
                evidence_url=item.get("<documented profile URL field>")),
            company_linkedin_url=item.get("<documented company LinkedIn URL field>"),
            company_website=item.get("<documented company website field>"),
            profile_url=item.get("<documented profile URL field>"))

    ACTORS = [ApifyEnrichmentActor(actor_id="<owner>~<actor>", job=COMPANY_TO_POCS,
                                   build_input=build_input, parse_item=parse_item,
                                   scrapes_linkedin=True)]
"""

ACTORS: list = []
