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
    zoominfo_api_key: str = ""
    zoominfo_api_url: str = ""
    apollo_api_key: str = ""
    apollo_api_url: str = "https://api.apollo.io/api/v1"
    zerobounce_api_key: str = ""
    zerobounce_api_url: str = "https://api.zerobounce.net/v2"

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
    qualify_threshold: float = 50.0
    #: Internal TA filter: this many distinct TA/recruiting staff => mature TA org.
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
    #: Three is a target, not a cap.
    contacts_target: int = 3
    contacts_max: int = 6
    contacts_for_review_companies: bool = True
    #: Each company costs ~30 page fetches; QUALIFIED companies go first.
    contacts_max_companies_per_run: int = 40
    #: Pause before re-checking a host that failed to resolve once.
    contacts_dns_retry_delay_seconds: float = 1.0
    contacts_homepage_max_links: int = 6
    contacts_sitemap_max_urls: int = 200
    contacts_sitemap_max_pages: int = 3
    #: careers./hr. subdomains from Certificate Transparency, ranked, bounded.
    contacts_scan_subdomains: bool = True
    contacts_subdomain_max_hosts: int = 3
    #: Public business directories, only after the company's own site is empty.
    contacts_search_directories: bool = True

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
