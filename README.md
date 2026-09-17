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
  -> Source registry ................. nexbase/discovery/registry.py
     Direct / ATS / Apify sources .... nexbase/discovery/jobspy_discovery.py,
                                       board_scrapers.py, ats_discovery.py
  -> Normalization ................... nexbase/pipeline/normalize.py
  -> Job deduplication ............... nexbase/pipeline/dedupe.py
  -> Freshness filter (<= 14 days) ... nexbase/pipeline/freshness.py
  -> Company identification + dedup .. nexbase/pipeline/company_identity.py, domain_resolver.py
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
| 2 | USA-wide planner + source registry | **done** |
| 3 | Direct sources + ATS + JobSpy cleanup | **done** |
| 4 | Normalization, dedup, freshness, company identity | **done** |
| 5 | Qualification engine | **done** |
| 6 | Free/public contact discovery | **done** |
| 7 | Enrichment waterfall ZoomInfo -> Apollo -> Apify | pending (not wired) |
| 8 | POC ranking, email verification, final lead, export | pending |
| 9 | Observability, source health, hardening | pending |

The runner currently executes: plan -> registry discovery -> normalize -> job dedup -> freshness ->
company identification -> qualify -> resolve domains -> persist -> free / public contact
discovery + POC ranking (QUALIFIED companies only). Paid enrichment and email verification
are **not called**.

## Planner and source registry

**Planner** (`DiscoveryPlanner().plan(sector, job)`): the only inputs.

- *Title variants* (up to `PLANNER_MAX_TITLE_VARIANTS`, the job first): other
  O*NET titles in the same occupation that share a distinctive word with the job,
  or that contain every word of it. "Welder" -> MIG Welder, TIG Welder, Welder
  Fitter. A vague ("Supervisor") or unknown job is searched as typed.
- *Geography*: the whole USA, then the 50 states + DC as the fallback partition.

**Registry** (`build_registry()`): one handler per portal; a second raises.

| Class | Portals | Handler |
|---|---|---|
| DIRECT | indeed, linkedin | JobSpy |
| DIRECT | simplyhired, talent_com, postjobfree | NexBase board adapters |
| ATS | greenhouse, lever, ashby, workable, smartrecruiters, bamboohr, breezy, jazzhr, recruitee, paylocity | ats-scrapers (disk-cached slices) |
| APIFY | glassdoor, zip_recruiter (off until `APIFY_API_TOKEN` is set) | Apify actors |

Every portal was verified live on 2026-09-16; the evidence, and why
ZipRecruiter and Glassdoor moved to Apify and Google Jobs and Monster were
removed, is in [reports/phase3_source_verification.json](reports/phase3_source_verification.json).
Every row a source returns must meet the Raw Job schema
(`core.models.raw_job_problems`); rows that do not are dropped and counted per
source (`schema_rejected`, `schema_problems`).

Every source has: enabled/disabled state (`SOURCES_DISABLED`), a minimum
interval between calls (`SOURCE_MIN_INTERVAL_SECONDS`, DIRECT/APIFY), a per-run
call budget (`SOURCE_MAX_CALLS_PER_RUN`), error tracking and metrics (reported
in `source_status` and `coverage`), and provenance stamped on every job (run,
source, class, handler, portal, title, geography, fetched_at), persisted in
`jobs.provenance` and `discovery_runs`.

Execution per source: every title nationwide first; a query that comes back
capped (`BUDGET_REACHED` / `SOURCE_LIMIT_REACHED`) fans out to all 51 state
cells, until the call budget is spent. Failed queries never fan out. One broken
source never stops the others.

The 14-day window reaches the sources themselves: JobSpy `hours_old`,
SimplyHired `t=14`, Talent.com `date=14`, Apify actor date inputs, and ATS
slices read with only rows inside the window. PostJobFree has no date filter;
the freshness stage handles it.

## Normalization, deduplication, freshness, company identity

- **Normalization** screens out postings with no employer name (`NO_COMPANY_NAME`)
  or outside the US (`NOT_US`), and parses location into city / state / country and the posting date into UTC
  with its precision (`DAY` for date-only sources, `TIME` for timestamps).
- **Job dedup** is conservative: the same `(source_site, external_id)` is one job;
  a cross-post needs the same company, title and a state-level location, and
  never joins two different employer domains. Duplicates are kept on the
  surviving job under `raw.duplicates`.
- **Freshness** keeps postings at most 14 days old (calendar days for `DAY`
  dates). An undated posting is kept only when its source enforced a date window
  of 14 days or less (`provenance.source_date_window_hours`); otherwise it is
  rejected as `MISSING_POSTING_DATE`. Every rejection is recorded with its reason.
- **Company identity** uses the strongest evidence first: website domain >
  employer (apply) URL domain with the same name > source company id (Indeed
  `/cmp/`, LinkedIn `/company/`, ATS tenant) > name + state. A name alone never
  merges: such a posting is its own `NAME_ONLY` company, and when another company
  shares the name it is flagged `AMBIGUOUS_COMPANY_IDENTITY` and sent to review.
  Weaker evidence never joins two different domains. `companies.identity_basis` / `identity_key` record
  which rule applied.

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
python -m nexbase.cli sectors --sector Manufacturing  # sectors + suggested jobs
python -m nexbase.cli sources                         # registry: class, handler, enabled
python -m nexbase.cli plan --sector Manufacturing --job "Warehouse Manager"
python -m nexbase.cli run --sector Manufacturing --job "Warehouse Manager" --dry-run
python -m nexbase.cli run ... --stop-at before_contacts   # skip contact discovery
python -m nexbase.cli export leads.csv --status QUALIFIED
```

## API

```bash
uvicorn nexbase.api.main:app --reload
```

| Endpoint | Auth |
|---|---|
| `GET /health` | public |
| `POST /pipeline/run` `{"sector", "job"}` | `X-API-Key` |
| `GET /leads` | `X-API-Key` |
| `GET /companies/{id}/emails` | `X-API-Key` |
| `GET /config/sectors` | `X-API-Key` |

`NEXBASE_API_KEY` is required unless `ENVIRONMENT` is a development value;
without it every authenticated endpoint returns 503. Inbound requests are
rate limited per caller (`NEXBASE_API_RATE_LIMIT`, default 60/min).

## Rules in force

- **Size:** <11 rejected, 11-200 eligible, >200 rejected, unknown -> `NEEDS_REVIEW`.
  A range that crosses a limit ("1 to 50", "more than 100") is `NEEDS_REVIEW`
  (`EMPLOYEE_SIZE_SPANS_LIMIT`).
- **Freshness:** postings older than 14 days, undated or future-dated are dropped.
- **Intermediaries:** staffing, recruiting, executive search and RPO are rejected
  on whole words in the company name or explicit self-description.
- **Internal TA:** the Master Plan names the filter but sets no thresholds. It uses
  the existing configuration only: TA/recruiting openings at or above
  `INTERNAL_TA_REJECT_THRESHOLD` (default 4), or a senior TA leader plus
  `INTERNAL_TA_REVIEW_THRESHOLD` (default 2), reject (`MATURE_INTERNAL_TA`); at or
  above the review threshold, `POSSIBLE_INTERNAL_TA` review.
- **Industry relevance** is judged against the run's selected sector, using the
  NexBase taxonomy as the source of truth (`data/processed/naics_to_industry.csv`).
  Only an industry label a source published about the employer counts; words in
  job descriptions do not. The label is placed in a sector through the official
  2022 NAICS titles (`nexbase/discovery/data/naics_titles.csv`, built by
  `scripts/build_industry_universe.py`): a label that is a sector name is that
  sector; otherwise the closest NAICS titles decide, and a tie between two sectors
  or a non-NexBase industry places nothing.
  - the label's sector is the selected sector -> relevant;
  - another sector, including a neighbouring one (Plastics/Rubber in a
    Manufacturing run, Warehousing in a Logistics run) -> `INDUSTRY_MISMATCH` review;
  - a label no sector can be read from -> `INDUSTRY_UNCLASSIFIED`; no label ->
    `INDUSTRY_UNKNOWN`; no sector -> `SECTOR_NOT_SELECTED` (all review, never a
    rejection).
- **Hiring signals** add points to a score that ranks companies and decides
  nothing: size and relevance, freshness, number of openings, growth wording,
  persistent hiring across runs, and LinkedIn applicants (<= 20 adds the most; a
  high or missing count adds nothing).
- **Outcomes:** any rule failure -> `REJECTED` with every failing reason; else any
  review flag -> `NEEDS_REVIEW`; else `QUALIFIED`. Reasons are stored on the
  company (`qualification_reasons`, `review_flags`, `qualification_breakdown`) and
  as one `qualification_reasons` row each with its `outcome`.

## Free / public contact discovery

Runs only for `QUALIFIED` companies, strongest hiring first, up to
`CONTACTS_MAX_COMPANIES_PER_RUN`; the rest are marked `SKIPPED_COMPANY_BUDGET`.
`NEEDS_REVIEW` and `REJECTED` companies are never searched.

1. **Emails already on the postings** (the sources' email fields and addresses in
   the description), stage `SAME_SOURCE`.
2. **Budgeted public page checks** through the Access Layer, at most
   `CONTACTS_MAX_PAGES_PER_COMPANY` fetches per company, stopping once
   `CONTACTS_TARGET` named decision-makers are found: the postings' own pages,
   employer profile pages the sources published, then the employer's own site
   (homepage navigation, conventional team/contact paths, sitemap, subdomains).
   Public directory search pages are off (`CONTACTS_SEARCH_DIRECTORIES=false`).
3. People are read from structured markup (JSON-LD, h-card/vcard, staff tables,
   named `mailto:` links, LinkedIn profile links) and, on the employer's own
   team / leadership / about / management pages only, from explicit plain-text
   pairs: "Name - Title", "Name, Title", "Name | Title", a heading or bold name
   with its title in the same card, or both lines in one element. The name must
   look like a person and the title must be on the POC list; error pages, blog
   posts, job pages, directories and the homepage are never read this way.
4. **POC ranking**, only people whose title is on the list are kept:
   - P1 Owner / CEO / President / Managing Partner
   - P2 COO / VP Operations / Director of Operations / General Manager
   - P3 HR Director / HR Manager / Head of HR / Head of People / Talent Acquisition
   - P4 Plant Manager / Operations Manager

   A division president ("President of Maintenance", "Regional President") is not P1.
   Titles match as whole words ("Vice President of Sales" is not a President,
   "HR Coordinator" is not a COO); "Assistant", "Associate", "Deputy" and "Former"
   titles are excluded. Within a tier: closeness to the roles being hired,
   extraction confidence, a published email, earlier discovery stage.
5. **Emails** are classified `ROLE` (shared: hr@, careers@, recruiting@, jobs@,
   info@, dispatch@ ...), `PERSONAL` (a named contact's) or `UNATTRIBUTED`, with
   `on_company_domain`.

Provenance: each contact row keeps its stage, source type, page URL and
extraction method, plus a `CONTACT` evidence row; each address is a
`company_email` evidence row with its type, source URL, portal and stage.

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

`nexbase/discovery/data/*.csv` (the planner's taxonomy) are generated from the BLS/NAICS/O*NET extracts in
`data/`:

```bash
python scripts/build_taxonomy.py
python scripts/build_industry_universe.py
```

## Known constraints

- **ats-scrapers full snapshot is ~16.9 GB.** Refused unless
  `ATS_ALLOW_FULL_SNAPSHOT=true`; named slices are used instead.
- **ATS slices** download once per dataset version to `ATS_CACHE_DIR`
  (~430 MB for all ten) and are read as US rows inside the freshness window.
  Lever, JazzHR, Breezy and Ashby mostly store country-only locations, so state
  queries cannot reach those rows; JazzHR rows are mostly undated.
- **Apify** actors are paid per result and were chosen from their documented
  output schemas; they have not been run by NexBase until a token is supplied.
- **LinkedIn applicant counts** are read from the public logged-out page only;
  a missing count never disqualifies a company.
- **Supabase direct connections**: use the Session pooler URI.
- **Evidence rows are not cascade-deleted** (polymorphic `record_id`), so the
  audit trail survives. `select prune_orphan_evidence();` purges orphans.
