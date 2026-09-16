# Changelog

Built phase by phase to the NexBase Master Plan. Each entry lists what was
removed, who depended on it, which tests covered it, and why it went.

Pre-Phase-1 snapshot: `Desktop/nex-base-backup-2026-09-16-pre-phase1.tar.gz`.
`ISSUE_LEDGER.md` is the history of the codebase before the Master Plan.

---

## Phase 2 — USA-wide planner + source registry (2026-09-16)

Tests: 709 before, **678 passed** after (0 failed). 29 new tests in
`tests/test_planner_registry.py`; 60 old tests removed because they pinned the
operator-configured input model the plan replaces (search terms, location,
size band, source lists as run input).

### Added

- **`nexbase/discovery/planner.py`** (rewritten): `DiscoveryPlanner().plan(sector,
  job)`. Title variants from the O*NET taxonomy (same SOC occupation + a shared
  distinctive word, or containing every word of the job); vague or unknown jobs
  are never widened. Geography generated internally: USA, then 50 states + DC.
- **`nexbase/discovery/registry.py`**: `SourceRegistry`, `Source`,
  `SourceClass` (DIRECT / ATS / APIFY), `build_registry()`. JobSpy is the DIRECT
  handler for indeed, linkedin, zip_recruiter, glassdoor, google; board adapters
  for simplyhired, talent_com, postjobfree, monster; ats-scrapers for 10 ATS
  slices; no APIFY sources yet. A second handler for a portal raises.
  Per source: enabled state, min interval, call budget, error tracking,
  metrics, provenance. Capped nationwide queries fan out to states.
- `RawJob.provenance` / `NormalizedJob.provenance`; `jobs.provenance jsonb`
  (additive schema change).
- Settings: `PLANNER_MAX_TITLE_VARIANTS=5`, `SOURCES_DISABLED=monster`,
  `SOURCE_MIN_INTERVAL_SECONDS=2.0`, `SOURCE_MAX_CALLS_PER_RUN=60`.
- CLI: `sources`, `plan --sector --job`; `run` now takes `--sector --job` only.

### Removed

| Removed | Callers / imports (verified) | Tests | Why |
|---|---|---|---|
| `DiscoveryConfig`, `build_config`, `DiscoveryProbe`, `is_nationwide`, nationwide aliases, `planner.record/close` | runner, api, cli | `test_user_controlled_discovery.py` (planning sections), `test_discovery.py` (3), `test_hardening.py` (sector/probe tests) | Plan: user input is Sector + Job only; the planner generates titles and geography |
| Runner `_discover`, `_discover_from_plan`, `_ats_probe`, `_ats_discovery`, `_resolve_ats_sites`; `run(ats_params, jobspy_params, board_params)` | api, flows (already gone), tests | `test_hardening.py` probe/ATS tests | Discovery now runs through the registry. `_resolve_ats_sites` moved to `registry.attach_ats_company_sites` |
| API run fields `raw_jobs`, `ats_params`, `jobspy_params`, `board_params`, `discovery`, `size_range`; `SizeRange`, `DiscoveryConfigRequest` | `/pipeline/run` | `test_hardening.py` (2 size-range tests) | Run input is `{sector, job}` |
| CLI `run --term --location --freshness-days --min-employees --max-employees` | `cli.py` | `test_cli_exposes_the_band_per_run` | Same |
| Settings `jobspy_default_sites`, `board_default_sites`, `ats_default_slices` (+ properties), `ats_max_probes`, `discovery_default_location` | jobspy/ats adapters, planner, runner, `live_smoke.py`, `sync_vault.py` | `test_discovery.py` (2), `test_hardening.py` (1) | Replaced by the registry catalog + `SOURCES_DISABLED` and the per-source call budget; one enabled/disabled mechanism |
| `DISABLED_BOARD_SCRAPERS`, `build_board_scrapers` | runner, `live_smoke.py`, `sync_vault.py` | `test_discovery.py` (2) | `BOARD_SCRAPERS` lists every board adapter; Monster is off via `SOURCES_DISABLED` |

### Changed

- `tests/test_user_controlled_discovery.py` → `tests/test_discovery_coverage.py`
  (coverage, pagination, JobSpy log capture and ATS slice-cache tests kept).
- `ATSDiscovery.search` records per-slice failures in `.errors`, so a failed
  slice reports `ERROR` instead of looking empty.
- Logs go to **stderr**, so CLI JSON on stdout is parseable.
- `/config/sectors` returns `suggested_jobs`.
- `scripts/live_smoke.py` runs one query through every enabled registry source
  (`--state` replaces `--location`); `scripts/sync_vault.py` reports the registry.

### Not verified live

No network run was made in this phase. Whether each portal accepts the
nationwide `USA` scope and state names, and how often queries cap and fan out,
is Phase 3's source verification.

---

## Phase 1 — Cleanup & foundation (2026-09-16)

Tests: 768 passed before, **709 passed** after (0 failed). 22 tests in
`test_hardening.py` and the whole of `test_enrichment_outreach.py` covered
removed behaviour; the rest were updated to new signatures and rules.

### Removed

| Removed | Callers / imports (verified) | Tests | Why |
|---|---|---|---|
| `nexbase/outreach/` (`queue.py`, `suppression.py`) | `pipeline/runner.py` step 9; `api/main.py` `/unsubscribe`, `/webhooks/bounce`; `cli.py` `suppress`, `optout`; `email/verification.py` auto-suppression | `test_enrichment_outreach.py`; `test_pipeline_e2e.py` acceptance 8 + opt-out; `test_hardening.py` unsubscribe escaping, suppression fail-closed | Outreach queue, unsubscribe tokens, bounce/complaint intake are explicit non-goals |
| `nexbase/flows/` (Prefect) | none — standalone entry point | cron/serve guard in `test_hardening.py` (kept, still true) | Not in the architecture. `prefect` dependency dropped |
| `nexbase/dashboard/` (Streamlit) | none — standalone app | 4 source-inspection tests in `test_hardening.py` | Not in the plan; output is CSV/API export. `streamlit` dependency dropped |
| `nexbase/enrichment/gate.py` | `pipeline/runner.py` step 8 | `test_enrichment_outreach.py`; `test_pipeline_e2e.py` acceptance 7; `test_hardening.py` stop-at-before-enrichment | Manual per-company human approval, and Apollo called as a cross-check even when ZoomInfo succeeded. Plan: automatic waterfall on QUALIFIED companies, Apollo only for missing fields (Phase 7). ZoomInfo/Apollo provider modules kept |
| `nexbase/db/stats.py` | `api/main.py` `/stats`; dashboard | none directly | Industry mix vs a 35/25/40 target plus outreach/approval counts. Conflicts with Sector+Job input. Funnel metrics are Phase 9 |
| Enrichment approval gate: `enrichment_approvals` table, `ApprovalStatus`, `/approvals` endpoints, `approvals` CLI, `verify_emails_requires_approval` | runner, api, cli, gate, stats, export | `test_hardening.py` (2 verification-approval tests); e2e | Human approval workflow is not in the plan |
| Review queue: `review_queue` / `review_history` tables, repository CRUD, `/reviews` endpoints, `runner._queue_for_review` | runner, api, dashboard | 7 tests in `test_hardening.py` | Human review workflow is not in the plan. `NEEDS_REVIEW` stays a persisted status with reasons |
| Industry allowlist / exclusions: `target_client_industries`, `discovery_excluded_sectors`, `discovery_excluded_soc_majors`; `profile.industry_excluded`, `OUT_OF_SCOPE`; `INDUSTRY_NOT_RELEVANT`, `INDUSTRY_NOT_TARGETED`; `taxonomy.eligible_industries`, `core_industries`, `non_core_industries`, `Industry.matches_exclusion` | `pipeline/qualification.py`, `profile.py`, `runner.py`, `scripts/sync_vault.py` | `test_hardening.py` (6), `test_soc_discovery.py` (6) | Named in the plan as "incorrect industry allowlists". Relevance will be judged against the run's selected sector (Phase 5) |
| Industry mix: `industry_mix_*` config, `PRIMARY_MIX_INDUSTRIES`, `report.industry_mix` | runner, stats, dashboard | e2e | A run targets one user-selected sector |
| Autonomous-discovery remnants: `taxonomy.sample_terms`, `terms_by_soc_major`, `soc_major_groups`, `US_METROS`, `US_STATES`, `suggested_locations` | tests and `sync_vault.py` only | `test_discovery.py` (4), `test_soc_discovery.py` (1), `test_user_controlled_discovery.py` (2) | Old autonomous sampling. Geography is generated internally (Phase 2) |
| `allow_oversize_with_approval` | `qualification.py` | `test_size_resolution.py` (2), `test_qualification.py` (1), e2e | Sent >200 to review pending approval. Plan: >200 is rejected |
| Config: `outreach_*`, `api_base_url`, `app_name`; size aliases `EMPLOYEE_SIZE_MIN`/`MIN_EMPLOYEES` and 4 alias properties | outreach, dashboard; cli/api | size-spelling tests in `test_hardening.py` | Served removed subsystems; one env name per setting (`SIZE_FILTER_MIN/MAX`) |
| Runner surface: `queue_outreach`, `stop_at` `before_enrichment` / `before_outreach`, lead fields `enriched`, `approval_status`, `provider_used`, `enrichment_credits`, `verified_emails`, `outreach_*` | runner, api, cli, flows | ~30 call sites passed `queue_outreach=False` | Those stages are no longer in the runner |
| Dead code: `repository.get_company`, `export.report_to_rows`, `config.reset_settings_cache`, 8 unused error classes (0 callers before Phase 1); `repository.count` (only caller was `stats.py`) | none | none | Dead |
| `industry_universe_audit.json`, `.coverage` | none | none | Stale artifact of the removed 80/20 exploration audit; coverage DB |

### Changed

- **`config.py`** rewritten: grouped by pipeline stage, defaults match the plan
  (`SIZE_FILTER_MIN=11`, `SIZE_FILTER_MAX=200`).
- **`.env`**: `EMPLOYEE_SIZE_MIN=10` → `SIZE_FILTER_MIN=11` (and the max line
  renamed). The old value contradicted the plan's 11-200 rule.
- **Qualification**: oversize is `REJECTED` (`EMPLOYEE_SIZE_ABOVE_MAX`); no global
  industry allowlist or exclusion.
- **Runner**: stops after free contact discovery + ranking. Paid enrichment and
  email verification are not called (providers kept for Phases 7-8). Each stage
  runs inside `logging_setup.stage`; the report gains `stages`; every log line of
  a run carries `run_id`.
- **Schema** (idempotent, additive, no DROP):
  - `rejection_reasons` renamed in place to `qualification_reasons` (+ `outcome`
    column, default `REJECTED`); repository method `insert_qualification_reason`.
  - No longer created: `enrichment_approvals`, `suppression_list`,
    `outreach_queue`, `review_queue`, `review_history`; columns
    `companies.approval_status/outreach_status/suppressed`,
    `contacts.outreach_status/suppressed`, `enrichment_logs.approval_id`.
    **Existing tables and columns in a live database are left untouched.**
- **API**: `/health`, `/pipeline/run`, `/leads`, `/companies/{id}/emails`,
  `/config/sectors`. **CLI**: `db`, `sectors`, `run`, `export`.
- **Export**: dropped approval/outreach/suppression columns.
- **`scripts/sync_vault.py`** (Stop hook) updated to the removed settings.
- `requirements.txt`: `-prefect`, `-streamlit`. Version 0.3.0.

### Known conflicts left for their own phase

| Item | Phase |
|---|---|
| ~~Run input still accepts search terms, location, size band and source lists~~ | done in 2 |
| ~~`discovery/` not yet split into planner + source registry~~ | done in 2 (as modules in `discovery/`) |
| JobSpy defaults still include `zip_recruiter`, `glassdoor`, `google` | 3 |
| Job dedup and company dedup run together, before freshness; P1/P2/P3 freshness tiers | 4 |
| `contacts_for_review_companies=True` (plan: contact discovery on QUALIFIED only) | 6 |
