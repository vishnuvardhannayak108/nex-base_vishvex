# Changelog

Built phase by phase to the NexBase Master Plan. Each entry lists what was
removed, who depended on it, which tests covered it, and why it went.

Pre-Phase-1 snapshot: `Desktop/nex-base-backup-2026-09-16-pre-phase1.tar.gz`.
`ISSUE_LEDGER.md` is the history of the codebase before the Master Plan.

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
| Run input still accepts search terms, location, size band and source lists (plan: Sector + Job only) | 2 |
| `discovery/` not yet split into planner + source registry | 2 |
| JobSpy defaults still include `zip_recruiter`, `glassdoor`, `google` | 3 |
| Job dedup and company dedup run together, before freshness; P1/P2/P3 freshness tiers | 4 |
| `contacts_for_review_companies=True` (plan: contact discovery on QUALIFIED only) | 6 |
