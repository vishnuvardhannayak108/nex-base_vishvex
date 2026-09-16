-- ============================================================================
-- NexBase canonical schema (Supabase / PostgreSQL 15)
-- Idempotent and additive: safe to run repeatedly, never drops data.
--
-- Tables:
--   companies                canonical company entity
--   jobs                     individual job postings (hiring signals)
--   contacts                 discovered/ranked people at a company
--   evidence                 raw observed facts linked to a record (provenance)
--   qualification_reasons    why a company/job was rejected or flagged, per stage
--   enrichment_logs          every paid enrichment call, with credits
--   email_verification_logs  every email verification result
--   discovery_runs           what was searched, when, and what it produced
--   company_hiring_history   per-run snapshot enabling "persistent hiring need"
--   audit_logs               structured event log (mirrors structlog)
-- ============================================================================

-- ---------------------------------------------------------------------------
-- companies
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS companies (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    normalized_name         text NOT NULL,
    normalized_domain       text NOT NULL,
    display_name            text,
    domain                  text,
    website                 text,
    location                text,
    location_normalized     text,
    country                 text,
    employee_size_min       integer,
    employee_size_max       integer,
    employee_size_source    text,
    industry                text,
    client_industry         text,
    industry_source         text,
    is_staffing_agency      boolean NOT NULL DEFAULT false,
    is_direct_employer      boolean,
    internal_ta_size        integer,
    internal_ta_verdict     text,
    qualification_status    text NOT NULL DEFAULT 'PENDING',
    qualification_score     numeric,
    qualification_reasons   jsonb,
    qualification_breakdown jsonb,
    review_flags            jsonb,
    hiring_intensity        integer NOT NULL DEFAULT 0,
    persistent_hiring_runs  integer NOT NULL DEFAULT 0,
    min_applicant_count     integer,
    source_type             text,
    source_priority         integer,
    first_seen_at           timestamptz NOT NULL DEFAULT now(),
    last_seen_at            timestamptz NOT NULL DEFAULT now(),
    raw_payload             jsonb,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_companies_domain_name UNIQUE (normalized_domain, normalized_name)
);

CREATE INDEX IF NOT EXISTS idx_companies_qualification ON companies (qualification_status);
CREATE INDEX IF NOT EXISTS idx_companies_domain ON companies (normalized_domain);
CREATE INDEX IF NOT EXISTS idx_companies_client_industry ON companies (client_industry);

-- Resolved official-domain provenance. `normalized_domain` stays the dedup key
-- and is never rewritten by resolution; `domain` carries the verified official
-- domain.
ALTER TABLE companies ADD COLUMN IF NOT EXISTS domain_confidence   text;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS domain_source       text;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS domain_evidence_url text;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS domain_resolved_at  timestamptz;

CREATE INDEX IF NOT EXISTS idx_companies_domain_confidence
    ON companies (domain_confidence) WHERE domain_confidence IS NOT NULL;

-- ---------------------------------------------------------------------------
-- jobs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id              uuid NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    external_id             text,
    title                   text,
    title_normalized        text,
    location                text,
    location_normalized     text,
    description             text,
    posting_date            date,
    posted_at               timestamptz,
    age_days                numeric,
    application_url         text,
    evidence_url            text,
    ats_platform            text,
    is_fresh                boolean,
    freshness_priority      integer,
    freshness_reason        text,
    applicant_count         integer,
    source_type             text,
    source_site             text,
    source_priority         integer,
    raw_payload             jsonb,
    first_seen_at           timestamptz NOT NULL DEFAULT now(),
    last_seen_at            timestamptz NOT NULL DEFAULT now(),
    created_at              timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_jobs_dedup UNIQUE (company_id, source_type, external_id)
);

-- Which registered source, handler and planned query found the posting.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS provenance jsonb;

CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs (company_id);
CREATE INDEX IF NOT EXISTS idx_jobs_posting_date ON jobs (posting_date);
CREATE INDEX IF NOT EXISTS idx_jobs_freshness ON jobs (freshness_priority);

-- ---------------------------------------------------------------------------
-- contacts
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contacts (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id              uuid NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    name                    text,
    title                   text,
    title_priority          integer,
    email                   text,
    rank_score              numeric,
    discovery_stage         text,
    profile_url             text,
    verification_status     text NOT NULL DEFAULT 'PENDING',
    verification_confidence numeric,
    source_type             text,
    source_priority         integer,
    raw_payload             jsonb,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_contacts_company_identity UNIQUE (company_id, name, title)
);

CREATE INDEX IF NOT EXISTS idx_contacts_company ON contacts (company_id);
CREATE INDEX IF NOT EXISTS idx_contacts_verification ON contacts (verification_status);
CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts (email);

-- ---------------------------------------------------------------------------
-- evidence (raw facts backing every decision/record)
--
-- `record_id` is a polymorphic reference (see `record_type`), so it carries no
-- foreign key and rows are NOT removed by cascade when their parent is
-- deleted: evidence survives as an audit trail. Use `prune_orphan_evidence()`
-- when a genuine purge is wanted.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evidence (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    record_type             text NOT NULL,
    record_id               uuid,
    key                     text NOT NULL,
    value                   text,
    url                     text,
    source_type             text,
    source_priority         integer,
    captured_at             timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_evidence_record ON evidence (record_type, record_id);

-- Provenance for company-level evidence (email type, discovery stage,
-- verification status).
ALTER TABLE evidence ADD COLUMN IF NOT EXISTS raw_payload jsonb;

CREATE INDEX IF NOT EXISTS idx_evidence_key ON evidence (key);

-- Opt-in cleanup for evidence whose parent record no longer exists.
CREATE OR REPLACE FUNCTION prune_orphan_evidence() RETURNS bigint AS $$
DECLARE
    removed bigint;
BEGIN
    WITH deleted AS (
        DELETE FROM evidence e
        WHERE (e.record_type = 'JOB' AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.id = e.record_id))
           OR (e.record_type = 'COMPANY' AND NOT EXISTS (SELECT 1 FROM companies c WHERE c.id = e.record_id))
           OR (e.record_type = 'CONTACT' AND NOT EXISTS (SELECT 1 FROM contacts k WHERE k.id = e.record_id))
        RETURNING 1
    )
    SELECT count(*) INTO removed FROM deleted;
    RETURN removed;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- qualification_reasons
--
-- Formerly `rejection_reasons`. Renamed in place so existing rows survive.
-- `outcome` is REJECTED or NEEDS_REVIEW.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('public.rejection_reasons') IS NOT NULL
       AND to_regclass('public.qualification_reasons') IS NULL THEN
        ALTER TABLE rejection_reasons RENAME TO qualification_reasons;
        ALTER INDEX IF EXISTS idx_rejection_company RENAME TO idx_qualification_reasons_company;
        ALTER INDEX IF EXISTS idx_rejection_stage RENAME TO idx_qualification_reasons_stage;
    END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS qualification_reasons (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id              uuid REFERENCES companies (id) ON DELETE CASCADE,
    job_id                  uuid REFERENCES jobs (id) ON DELETE CASCADE,
    stage                   text NOT NULL,
    reason                  text NOT NULL,
    details                 jsonb,
    created_at              timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE qualification_reasons
    ADD COLUMN IF NOT EXISTS outcome text NOT NULL DEFAULT 'REJECTED';

CREATE INDEX IF NOT EXISTS idx_qualification_reasons_company ON qualification_reasons (company_id);
CREATE INDEX IF NOT EXISTS idx_qualification_reasons_stage ON qualification_reasons (stage);

-- ---------------------------------------------------------------------------
-- enrichment_logs (every paid enrichment call)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS enrichment_logs (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id              uuid REFERENCES companies (id) ON DELETE CASCADE,
    contact_id              uuid REFERENCES contacts (id) ON DELETE CASCADE,
    provider                text NOT NULL,
    endpoint                text,
    payload                 jsonb,
    result                  jsonb,
    status                  text NOT NULL DEFAULT 'SUCCESS',
    billable                boolean NOT NULL DEFAULT true,
    credit_cost             numeric NOT NULL DEFAULT 0,
    created_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_enrichment_company ON enrichment_logs (company_id);
CREATE INDEX IF NOT EXISTS idx_enrichment_provider ON enrichment_logs (provider);
CREATE INDEX IF NOT EXISTS idx_enrichment_billable ON enrichment_logs (billable);

-- ---------------------------------------------------------------------------
-- email_verification_logs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS email_verification_logs (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id              uuid REFERENCES contacts (id) ON DELETE CASCADE,
    company_id              uuid REFERENCES companies (id) ON DELETE CASCADE,
    email                   text,
    provider                text NOT NULL,
    status                  text NOT NULL,
    confidence              numeric,
    raw_result              jsonb,
    created_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_emailver_contact ON email_verification_logs (contact_id);
CREATE INDEX IF NOT EXISTS idx_emailver_email ON email_verification_logs (email);

-- ---------------------------------------------------------------------------
-- discovery_runs (what was searched, when, and what came back)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS discovery_runs (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_key                 text,
    source                  text NOT NULL,
    search_term             text,
    location                text,
    client_industry         text,
    params                  jsonb,
    jobs_found              integer NOT NULL DEFAULT 0,
    companies_found         integer NOT NULL DEFAULT 0,
    qualified               integer NOT NULL DEFAULT 0,
    status                  text NOT NULL DEFAULT 'COMPLETE',
    error                   text,
    started_at              timestamptz NOT NULL DEFAULT now(),
    finished_at             timestamptz
);

CREATE INDEX IF NOT EXISTS idx_discovery_runs_key ON discovery_runs (run_key);
CREATE INDEX IF NOT EXISTS idx_discovery_runs_started ON discovery_runs (started_at);

-- ---------------------------------------------------------------------------
-- company_hiring_history (enables "persistent hiring need" across runs)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS company_hiring_history (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id              uuid NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    observed_on             date NOT NULL DEFAULT current_date,
    open_jobs               integer NOT NULL DEFAULT 0,
    fresh_jobs              integer NOT NULL DEFAULT 0,
    created_at              timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_hiring_history UNIQUE (company_id, observed_on)
);

CREATE INDEX IF NOT EXISTS idx_hiring_history_company ON company_hiring_history (company_id);

-- ---------------------------------------------------------------------------
-- audit_logs (structured event log)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_logs (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event                   text NOT NULL,
    entity_type             text,
    entity_id               uuid,
    message                 text,
    data                    jsonb,
    level                   text NOT NULL DEFAULT 'INFO',
    created_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_logs (event);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_logs (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs (created_at);
CREATE INDEX IF NOT EXISTS idx_audit_level ON audit_logs (level);

-- ---------------------------------------------------------------------------
-- updated_at maintenance
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['companies', 'contacts'] LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS trg_%1$s_updated_at ON %1$s', t
        );
        EXECUTE format(
            'CREATE TRIGGER trg_%1$s_updated_at BEFORE UPDATE ON %1$s '
            'FOR EACH ROW EXECUTE FUNCTION set_updated_at()', t
        );
    END LOOP;
END;
$$;

-- ---------------------------------------------------------------------------
-- Row level security
--
-- Supabase publishes every table in `public` through PostgREST to the project's
-- anon key. RLS is enabled with NO permissive policy, so PostgREST sees nothing
-- and the service-role key - which bypasses RLS by design - stays the only way
-- in. That is what the server-side pipeline uses.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'companies', 'jobs', 'contacts', 'evidence', 'qualification_reasons',
        'enrichment_logs', 'email_verification_logs', 'discovery_runs',
        'company_hiring_history', 'audit_logs'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    END LOOP;
END;
$$;
