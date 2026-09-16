# NexBase — US-wide B2B Lead Discovery + Contact Intelligence

Input: **Sector + Job** (e.g. `Manufacturing` + `Warehouse Manager`).
NexBase searches the USA for companies hiring for that job, qualifies them,
finds decision-makers, and produces outreach-ready leads for **export**.

**NexBase does not send email.** No outreach, sequences, Instantly, bounce
handling or warm-up. It stops at CSV / API export.

Nothing is ever fabricated: company names, sizes, posting dates, applicant
counts and contact details are recorded only when observed; missing facts
stay null.

---

## Architecture → modules

```
USER (Sector + Job)
  -> USA-wide planner ................ nexbase/discovery/planner.py, taxonomy.py
  -> Direct / ATS / Apify sources .... nexbase/discovery/jobspy_discovery.py,
                                       board_scrapers.py, ats_discovery.py
  -> Normalization ................... nexbase/pipeline/normalize.py
  -> Job deduplication ............... nexbase/pipeline/dedupe.py
  -> Freshness filter (<= 14 days) ... nexbase/pipeline/freshness.py
  -> Company identification + dedup .. nexbase/pipeline/dedupe.py, domain_resolver.py
  -> Qualification engine ............ nexbase/pipeline/qualification.py, profile.py
  -> Free / public contact discovery . nexbase/contacts/discovery.py, extraction.py,
                                       nexbase/email/discovery.py
  -> ZoomInfo -> Apollo -> Apify ..... nexbase/enrichment/ (providers only, not wired)
  -> POC ranking ..................... nexbase/contacts/ranking.py
  -> Email verification .............. nexbase/email/verification.py (not wired)
  -> Final lead + export ............. nexbase/pipeline/runner.py, nexbase/export.py,
                                       GET /leads
```

Shared infrastructure:

| Module | Role |
|---|---|
| `nexbase/config.py` | Pydantic Settings, every value defaulted |
| `nexbase/logging_setup.py` | structlog JSON logs, `bind_run` run id, `stage` timer |
| `nexbase/access/` | fetch layer: robots.txt, SSRF guard, rate limits, Camoufox budget |
| `nexbase/db/` | Supabase client, `schema.sql`, repository |
| `nexbase/core/` | shared models, enums, errors |
| `nexbase/api/main.py`, `nexbase/cli.py` | entry points |

### Build status (Master Plan)

| Phase | Scope | State |
|---|---|---|
| 1 | Cleanup, config, schema, logging/observability skeleton | **done** |
| 2 | USA-wide planner + source registry | pending |
| 3 | Direct sources + ATS + JobSpy cleanup | pending |
| 4 | Normalization, dedup, freshness | pending (existing code runs) |
| 5 | Qualification engine | pending (existing code runs) |
| 6 | Free/public contact discovery | pending (existing code runs) |
| 7 | Enrichment waterfall ZoomInfo -> Apollo -> Apify | pending (not wired) |
| 8 | POC ranking, email verification, final lead, export | pending |
| 9 | Observability, source health, hardening | pending |

The runner currently executes: discover -> normalize -> dedupe -> freshness ->
qualify -> resolve domains -> free contact discovery + ranking. Paid enrichment
and email verification are **not called** until their budget logic exists.

---

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env      # fill in Supabase + provider credentials
python -m nexbase.cli db apply-schema
python -m nexbase.cli db health
```

## CLI

```bash
python -m nexbase.cli db apply-schema                 # idempotent, additive
python -m nexbase.cli db health                       # every table reachable
python -m nexbase.cli sectors --sector Manufacturing  # sectors + title variants
python -m nexbase.cli run --sector Manufacturing --term "Warehouse Manager" --dry-run
python -m nexbase.cli run ... --stop-at before_contacts
python -m nexbase.cli export leads.csv --status QUALIFIED
```

## API

```bash
uvicorn nexbase.api.main:app --reload
```

| Endpoint | Auth |
|---|---|
| `GET /health` | public |
| `POST /pipeline/run` | `X-API-Key` |
| `GET /leads` | `X-API-Key` |
| `GET /companies/{id}/emails` | `X-API-Key` |
| `GET /config/sectors` | `X-API-Key` |

`NEXBASE_API_KEY` is required unless `ENVIRONMENT` is a development value;
without it every authenticated endpoint returns 503. Inbound requests are
rate limited per caller (`NEXBASE_API_RATE_LIMIT`, default 60/min).

## Rules in force

- **Size:** <11 rejected, 11-200 eligible, >200 rejected, unknown -> `NEEDS_REVIEW`.
- **Freshness:** postings older than 14 days, undated or future-dated are dropped.
- **Intermediaries:** staffing, recruiting, executive search and RPO are rejected
  on the company name or explicit self-description.
- **Outcomes:** `QUALIFIED` / `NEEDS_REVIEW` / `REJECTED`, always with reasons
  (`companies.qualification_reasons`, `review_flags`, and `qualification_reasons`).

## Observability

Every run gets a `run_id` bound to all of its log lines. Each stage logs
`stage_start` / `stage_complete` (or `stage_failed`) with `duration_ms`, and the
run report carries `stages`, per-source `source_status` and `coverage`.

## Fetching

Every self-driven fetch passes two gates first: an SSRF guard (public http(s)
addresses only, redirects revalidated) and robots.txt. JobSpy and `ats-scrapers`
use their own HTTP clients and are outside that guarantee.

## Database

`nexbase/db/schema.sql` is idempotent and additive; it never drops data. Row
level security is enabled and forced on every table with no permissive policy,
so only the service-role key can read or write.

Tables: `companies`, `jobs`, `contacts`, `evidence`, `qualification_reasons`,
`enrichment_logs`, `email_verification_logs`, `discovery_runs`,
`company_hiring_history`, `audit_logs`.

## Tests

```bash
python -m pytest              # no network, no database
python -m pytest -m network   # the one live integration guard, opt-in
```

## Regenerating the taxonomy

`nexbase/discovery/data/*.csv` are generated from the BLS/NAICS/O*NET extracts in
`data/`:

```bash
python scripts/build_taxonomy.py
python scripts/build_industry_universe.py
```

## Known constraints

- **ats-scrapers full snapshot is ~16.9 GB.** Refused unless
  `ATS_ALLOW_FULL_SNAPSHOT=true`; named slices are used instead.
- **LinkedIn applicant counts** are read from the public logged-out page only;
  a missing count never disqualifies a company.
- **Supabase direct connections**: use the Session pooler URI.
- **Evidence rows are not cascade-deleted** (polymorphic `record_id`), so the
  audit trail survives. `select prune_orphan_evidence();` purges orphans.
