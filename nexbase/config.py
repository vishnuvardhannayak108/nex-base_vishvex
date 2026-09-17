"""Central configuration via pydantic-settings.

Reads from environment variables and an optional ``.env`` file. Every value
has a safe default; secrets are empty until supplied.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # General
    # ------------------------------------------------------------------
    environment: str = "development"
    log_level: str = "INFO"

    # ------------------------------------------------------------------
    # Supabase
    # ------------------------------------------------------------------
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_db_url: str = ""
    #: A failed write raises instead of being swallowed. Silent write failures
    #: are how an unapplied schema masquerades as a successful run.
    db_strict: bool = True

    # ------------------------------------------------------------------
    # Paid providers (enrichment runs only on QUALIFIED companies)
    # ------------------------------------------------------------------
    #: "username:password" (documented /authenticate) or a pre-issued JWT.
    zoominfo_api_key: str = ""
    zoominfo_api_url: str = ""
    #: QUALIFIED companies ZoomInfo may be consulted for in one run.
    zoominfo_max_companies_per_run: int = 25
    #: Contact Enrich records (one credit each) per company.
    zoominfo_max_contact_enrich_per_company: int = 3
    #: A company consulted within this many days reuses its stored ZoomInfo contacts.
    zoominfo_refresh_days: int = 90
    #: Apollo is a FALLBACK: consulted only when ZoomInfo could not supply what
    #: was required. QUALIFIED companies per run, People Enrichment lookups per
    #: company, and the window in which its stored contacts are reused.
    apollo_max_companies_per_run: int = 25
    apollo_max_person_lookups_per_company: int = 3
    apollo_refresh_days: int = 90
    apollo_api_key: str = ""
    apollo_api_url: str = "https://api.apollo.io/api/v1"
    zerobounce_api_key: str = ""
    zerobounce_api_url: str = "https://api.zerobounce.net/v2"
    #: Paid verification of ranked POC emails. Off until the client approves
    #: automatic paid verification.
    email_verification_enabled: bool = False
    email_verification_max_per_run: int = 50
    #: A VALID / INVALID / RISKY verdict younger than this is reused.
    email_verification_refresh_days: int = 30

    # ------------------------------------------------------------------
    # Freshness: keep postings at most this many days old
    # ------------------------------------------------------------------
    freshness_max_days: int = 14

    # ------------------------------------------------------------------
    # Qualification
    # ------------------------------------------------------------------
    #: Eligible employee band: <11 reject, 11-200 eligible, >200 reject,
    #: unknown -> NEEDS_REVIEW.
    size_filter_min: int = 11
    size_filter_max: int = 200
    #: Internal TA filter. The Master Plan sets no numbers: these are the
    #: existing defaults, not plan rules. TA/recruiting openings at or above
    #: the reject threshold (or a senior TA leader plus the review threshold)
    #: reject; at or above the review threshold, review.
    internal_ta_reject_threshold: int = 4
    internal_ta_review_threshold: int = 2
    #: LinkedIn applicant signal: at or below this is positive, never a reject.
    linkedin_max_applicants: int = 20

    #: Public-web headcount crawling. Off: a live diagnostic read 24 pages
    #: across 4 domains and found 0 explicit statements.
    size_resolver_enabled: bool = False
    size_resolver_max_pages: int = 8
    size_cache_max_age_days: int = 90

    # ------------------------------------------------------------------
    # Company identification (official domain)
    # ------------------------------------------------------------------
    domain_resolver_enabled: bool = True
    domain_max_candidates: int = 4
    #: Companies whose domain may be searched for in one run. Cached and
    #: already-known domains do not count against it.
    domain_resolve_max_per_run: int = 150
    domain_resolve_max_seconds: float = 600.0
    #: A persisted domain is reused rather than re-searched until this old.
    domain_cache_max_age_days: int = 180
    #: A failed attempt is not retried for this long. A cooldown, not a verdict.
    domain_negative_cache_days: int = 7
    #: Subdomain discovery is a fallback when the main site yields no evidence.
    subdomain_fallback_enabled: bool = True
    subdomain_max_checked: int = 4
    subdomain_page_budget: int = 4

    # ------------------------------------------------------------------
    # Free / public contact discovery + POC ranking
    # ------------------------------------------------------------------
    #: Runs on QUALIFIED companies only. Three is a target, not a cap.
    contacts_target: int = 3
    contacts_max: int = 6
    #: Companies whose contacts are searched in one run, best first.
    contacts_max_companies_per_run: int = 40
    #: Page fetches one company may use, every stage included.
    contacts_max_pages_per_company: int = 30
    #: Pause before re-checking a host that failed to resolve once.
    contacts_dns_retry_delay_seconds: float = 1.0
    contacts_homepage_max_links: int = 6
    contacts_sitemap_max_urls: int = 200
    contacts_sitemap_max_pages: int = 3
    #: careers./hr. subdomains from Certificate Transparency, ranked, bounded.
    contacts_scan_subdomains: bool = True
    contacts_subdomain_max_hosts: int = 3
    #: Public business directories, only after the company's own site is empty.
    #: Off: a directory search page lists other businesses, so an address on it
    #: cannot be attributed to this one.
    contacts_search_directories: bool = False

    # ------------------------------------------------------------------
    # Access layer (every self-driven fetch)
    # ------------------------------------------------------------------
    user_agent_token: str = "NexBaseBot"
    respect_robots: bool = True
    max_retries: int = 2
    request_timeout_seconds: int = 30
    rate_limit_per_second: float = 2.0
    #: Hard ceiling on Camoufox launches per run.
    camoufox_budget_per_run: int = 25
    camoufox_settle_seconds: float = 3.0
    #: Fallbacks one host may use before its cooldown starts.
    camoufox_host_max_fallbacks: int = 6
    camoufox_domain_cooldown_seconds: float = 300.0
    #: Consecutive transport failures before a host is skipped for the run.
    host_failure_threshold: int = 3

    # ------------------------------------------------------------------
    # USA-wide planner
    # ------------------------------------------------------------------
    #: Titles searched per run, the user's job included.
    planner_max_title_variants: int = 5

    # ------------------------------------------------------------------
    # Source registry
    # ------------------------------------------------------------------
    #: Portals switched off, comma-separated (e.g. "glassdoor,greenhouse").
    #: Every other registered portal runs.
    sources_disabled: str = ""
    #: Minimum seconds between two calls to the same DIRECT or APIFY source.
    source_min_interval_seconds: float = 2.0
    #: Queries one source may run per pipeline run, state fan-out included.
    #: Hitting it is reported as BUDGET_REACHED.
    source_max_calls_per_run: int = 60

    #: Apify sources run only with a token. Each call is paid per result, so
    #: an Apify source gets its own, much smaller call budget and hard caps on
    #: items and dollars per call.
    apify_api_token: str = ""
    apify_max_calls_per_run: int = 5
    apify_max_items_per_query: int = 100
    apify_max_charge_usd_per_call: float = 1.0
    apify_timeout_seconds: int = 300
    #: Apify contact enrichment is the LAST fallback, configured per job with a
    #: primary and an optional backup actor. An id is used only if that actor is
    #: registered in enrichment/apify.py for the same job; none is registered,
    #: and no actor has been selected (live validation pending client credentials).
    apify_company_to_pocs_actor: str = ""
    apify_company_to_pocs_backup_actor: str = ""
    apify_profile_to_email_actor: str = ""
    apify_profile_to_email_backup_actor: str = ""
    #: Deferred: Phase 6 already discovers public employer-site emails. Never run.
    apify_website_to_emails_actor: str = ""
    apify_website_to_emails_backup_actor: str = ""
    apify_enrichment_max_companies_per_run: int = 5
    apify_enrichment_max_items: int = 10

    # ------------------------------------------------------------------
    # Discovery source adapters
    # ------------------------------------------------------------------
    discovery_country: str = "usa"
    #: NexBase coverage budgets per (source, query). Hitting one is reported as
    #: BUDGET_REACHED, never confused with the source running out.
    discovery_max_pages_per_query: int = 25
    discovery_max_results_per_query: int = 1000

    jobspy_results_wanted: int = 50
    #: Fetch a posting's own page when the results card had no description.
    board_fetch_detail_pages: bool = True
    board_detail_page_budget: int = 10

    #: ats-scrapers with no slice downloads a ~17 GB snapshot. Opt-in only.
    ats_allow_full_snapshot: bool = False
    #: Downloaded ATS slices, one file per dataset version. Keep it out of any
    #: synced folder: the slices together are hundreds of MB.
    ats_cache_dir: str = "~/.cache/nexbase/ats"
    #: The ATS jobs dataset has no website column; fill it from the directory.
    ats_resolve_company_sites: bool = True

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_role_key)

    @property
    def disabled_sources(self) -> frozenset[str]:
        return frozenset(
            s.strip().lower() for s in self.sources_disabled.split(",") if s.strip())


@lru_cache
def get_settings() -> Settings:
    """Return a cached, shared Settings instance."""
    return Settings()
