# Apify enrichment actor benchmark

Framework for comparing shortlisted Apify enrichment actors **before** any is
selected. It has **not been run**: there is no Apify credential, and live
validation is **PENDING CLIENT CREDENTIALS**.

- `companies.example.json` - format of the company fixture set. Replace with
  10-20 real NexBase-style companies (verified domains; optional `known_people`
  with LinkedIn profile URLs for `profile_to_email`).
- `actors_template.py` - how to define a candidate actor for the benchmark. It
  contains no actors. Definitions must follow each actor's documented schema and
  are benchmarked only, never registered for production.
- Runner: `scripts/benchmark_apify_actors.py` (refuses to run without
  `APIFY_API_TOKEN`, actor definitions and `--confirm-paid-runs`).
- Metrics: `nexbase/enrichment/apify_benchmark.py` - POC coverage,
  correct-company rate, P1-P4 title match rate, email classes, duplicate rate,
  runtime, provider errors, cost cap and actual Apify usage when reported,
  identity statuses, risks.

Jobs benchmarked: `company_to_pocs`, `profile_to_email`. `website_to_emails` is
deferred: Phase 6 already discovers public employer-site emails; revisit only if
a benchmark shows meaningful incremental coverage (e.g. JS-heavy pages the
crawler misses).

**Risk:** actors that scrape LinkedIn are flagged
`LINKEDIN_SCRAPING_REQUIRES_CLIENT_APPROVAL`. Whether they may be used in
production is a client/business decision, not a technical one.
