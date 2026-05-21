-- AI Workflow Demo — initial schema
-- Migration: 0001_evidence_schema.sql
-- Target: PostgreSQL 16.x
--
-- Creates:
--   * roles: evidence_writer, evidence_reader (least-privilege)
--   * enums: severity, confidence_band, disposition, custody_event_type,
--            collection_method, source_system
--   * tables: cases, findings, evidence_items, evidence_custody, audit_log
--   * trigger: blocks UPDATE/DELETE on evidence_custody (append-only)
--
-- Notes:
--   * Hash chain is computed in application code (see src/evidence/custody.py).
--     The DB enforces immutability; the application enforces correctness of the
--     chain. Both checks must pass during verify_evidence().
--   * All timestamps are timestamptz. The application writes UTC; the DB stores
--     in UTC because the container sets TZ=UTC and PGTZ=UTC.
--   * pgcrypto is enabled for gen_random_uuid() (used as a fallback when the
--     application does not supply UUIDv7).

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- Roles
-- ---------------------------------------------------------------------------
-- Passwords are set by the bootstrap script outside this migration so we do
-- not bake them into source. CREATE ROLE IF NOT EXISTS is not supported, so
-- we use a DO block.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evidence_writer') THEN
        CREATE ROLE evidence_writer NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evidence_reader') THEN
        CREATE ROLE evidence_reader NOLOGIN;
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------------

CREATE TYPE severity AS ENUM ('low', 'medium', 'high', 'critical');

-- Mirrors the project rule: 1-39 / 40-69 / 70-89 / 90-100
CREATE TYPE confidence_band AS ENUM ('weak', 'mixed', 'strong', 'multi_source');

CREATE TYPE disposition AS ENUM ('true_positive', 'false_positive', 'inconclusive', 'open');

CREATE TYPE custody_event_type AS ENUM (
    'collected',
    'accessed',
    'exported',
    'transferred',
    'disposed'
);

CREATE TYPE collection_method AS ENUM ('api', 'export', 'kql', 'spl', 'manual');

CREATE TYPE source_system AS ENUM (
    'google_workspace.admin_reports',
    'google_workspace.drive',
    'google_workspace.gmail',
    'crowdstrike.fdr',
    'crowdstrike.alerts',
    'mimecast.incydr',
    'checkpoint.dlp',
    'microsoft.sentinel',
    'elastic.logs',
    'ad.windows_events',
    'other'
);

-- ---------------------------------------------------------------------------
-- Cases
-- ---------------------------------------------------------------------------

CREATE TABLE cases (
    case_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_code        TEXT NOT NULL UNIQUE,                       -- human-friendly, e.g. IT-2026-014
    title            TEXT NOT NULL CHECK (length(title) <= 256),
    -- Pseudonymous reference to the subject. The mapping subject_ref -> identity
    -- lives in a separately controlled schema (out of scope for the demo).
    subject_ref      TEXT NOT NULL,
    opened_at_utc    TIMESTAMPTZ NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
    closed_at_utc    TIMESTAMPTZ,
    disposition      disposition NOT NULL DEFAULT 'open',
    severity         severity,
    confidence_score SMALLINT CHECK (confidence_score BETWEEN 1 AND 100),
    confidence_band  confidence_band,
    notes            TEXT
);

CREATE INDEX cases_opened_at_idx ON cases (opened_at_utc DESC);
CREATE INDEX cases_disposition_idx ON cases (disposition);

-- ---------------------------------------------------------------------------
-- Findings
-- ---------------------------------------------------------------------------

CREATE TABLE findings (
    finding_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id           UUID NOT NULL REFERENCES cases (case_id) ON DELETE RESTRICT,
    title             TEXT NOT NULL,
    description       TEXT NOT NULL,
    mitre_ttps        TEXT[] NOT NULL DEFAULT '{}',     -- e.g. {T1078,T1567.002}
    severity          severity NOT NULL,
    confidence_score  SMALLINT NOT NULL CHECK (confidence_score BETWEEN 1 AND 100),
    confidence_band   confidence_band NOT NULL,
    disposition       disposition NOT NULL DEFAULT 'open',
    single_source     BOOLEAN NOT NULL DEFAULT FALSE,   -- corroboration flag
    created_at_utc    TIMESTAMPTZ NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
    updated_at_utc    TIMESTAMPTZ NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')
);

CREATE INDEX findings_case_id_idx ON findings (case_id);
CREATE INDEX findings_severity_idx ON findings (severity);

-- ---------------------------------------------------------------------------
-- Evidence items (one row per stored artefact)
-- ---------------------------------------------------------------------------

CREATE TABLE evidence_items (
    artefact_id         UUID PRIMARY KEY,
    case_id             UUID NOT NULL REFERENCES cases (case_id) ON DELETE RESTRICT,
    source_system       source_system NOT NULL,
    collection_method   collection_method NOT NULL,
    query               TEXT NOT NULL,
    collector_principal TEXT NOT NULL,
    collected_at_utc    TIMESTAMPTZ NOT NULL,
    original_tz         TEXT NOT NULL,
    bytes               BIGINT NOT NULL CHECK (bytes >= 0),
    sha256              BYTEA NOT NULL CHECK (octet_length(sha256) = 32),
    mime_type           TEXT NOT NULL,
    s3_uri              TEXT NOT NULL,
    ecs_index           TEXT,
    ecs_doc_id          TEXT,
    pii_tags            TEXT[] NOT NULL DEFAULT '{}',
    retention_class     TEXT NOT NULL,
    manifest_uri        TEXT NOT NULL,                     -- pointer to the .manifest.json
    signing_key_id      TEXT NOT NULL,                     -- public-key fingerprint
    manifest_version    SMALLINT NOT NULL,
    created_at_utc      TIMESTAMPTZ NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')
);

CREATE INDEX evidence_items_case_id_idx ON evidence_items (case_id);
CREATE INDEX evidence_items_source_idx ON evidence_items (source_system);
CREATE INDEX evidence_items_collected_at_idx ON evidence_items (collected_at_utc DESC);

-- ---------------------------------------------------------------------------
-- Evidence custody ledger (append-only, hash-chained)
-- ---------------------------------------------------------------------------

CREATE TABLE evidence_custody (
    event_id        UUID PRIMARY KEY,
    artefact_id     UUID NOT NULL REFERENCES evidence_items (artefact_id) ON DELETE RESTRICT,
    event_type      custody_event_type NOT NULL,
    actor           TEXT NOT NULL,
    actor_ip        INET,
    host            TEXT,
    purpose         TEXT NOT NULL,
    event_time_utc  TIMESTAMPTZ NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
    prev_event_hash BYTEA NOT NULL CHECK (octet_length(prev_event_hash) = 32),
    this_event_hash BYTEA NOT NULL CHECK (octet_length(this_event_hash) = 32),
    signature       BYTEA NOT NULL,
    signing_key_id  TEXT NOT NULL
);

CREATE INDEX evidence_custody_artefact_idx
    ON evidence_custody (artefact_id, event_time_utc);

-- Append-only trigger: forbid UPDATE and DELETE for every role.
-- The error message is intentionally explicit so the tamper-demo script can
-- assert on it.
CREATE OR REPLACE FUNCTION block_custody_modify() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'evidence_custody is append-only: % is not permitted', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER evidence_custody_no_update
    BEFORE UPDATE ON evidence_custody
    FOR EACH ROW EXECUTE FUNCTION block_custody_modify();

CREATE TRIGGER evidence_custody_no_delete
    BEFORE DELETE ON evidence_custody
    FOR EACH ROW EXECUTE FUNCTION block_custody_modify();

-- TRUNCATE is also a deletion vector — block it.
CREATE TRIGGER evidence_custody_no_truncate
    BEFORE TRUNCATE ON evidence_custody
    FOR EACH STATEMENT EXECUTE FUNCTION block_custody_modify();

-- ---------------------------------------------------------------------------
-- Agent audit log (every query / data access by the workflow)
-- ---------------------------------------------------------------------------

CREATE TABLE audit_log (
    audit_id        BIGSERIAL PRIMARY KEY,
    event_time_utc  TIMESTAMPTZ NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,           -- e.g. 'record_evidence', 'verify_evidence'
    target          TEXT,                    -- artefact_id, case_id, etc.
    outcome         TEXT NOT NULL,           -- 'ok' | 'fail'
    details         JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX audit_log_actor_idx ON audit_log (actor, event_time_utc DESC);
CREATE INDEX audit_log_action_idx ON audit_log (action);

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

-- evidence_writer: INSERT on custody + evidence_items + audit_log,
-- SELECT on cases (to look up case existence). No UPDATE/DELETE anywhere.
GRANT USAGE ON SCHEMA public TO evidence_writer;
GRANT SELECT ON cases TO evidence_writer;
GRANT INSERT, SELECT ON evidence_items TO evidence_writer;
GRANT INSERT, SELECT ON evidence_custody TO evidence_writer;
GRANT INSERT, SELECT ON audit_log TO evidence_writer;
GRANT USAGE, SELECT ON SEQUENCE audit_log_audit_id_seq TO evidence_writer;

-- evidence_reader: SELECT only.
GRANT USAGE ON SCHEMA public TO evidence_reader;
GRANT SELECT ON cases, findings, evidence_items, evidence_custody, audit_log
    TO evidence_reader;

COMMIT;
