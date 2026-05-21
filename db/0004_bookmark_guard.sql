-- AI Workflow Demo — bookmark guard schema
-- Migration: 0004_bookmark_guard.sql
-- Target: PostgreSQL 16.x
-- Depends on: 0001_evidence_schema.sql
--
-- Creates:
--   * table: bookmark_violations
--   * grants: evidence_writer INSERT/SELECT; evidence_reader SELECT

BEGIN;

-- ---------------------------------------------------------------------------
-- bookmark_violations
--
-- One row per detected sensitive-URL bookmark or homepage entry, written by
-- the bookmark_guard automation after detection and removal. Serves as the
-- audit trail showing the employee attempted to persist a sensitive URL in
-- their browser.
-- ---------------------------------------------------------------------------

CREATE TABLE bookmark_violations (
    violation_id     UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    detected_at_utc  TIMESTAMPTZ NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
    hostname         TEXT        NOT NULL,
    os_username      TEXT        NOT NULL,
    chrome_profile   TEXT        NOT NULL,   -- profile dir name, e.g. "Default"
    chrome_email     TEXT,                   -- profile account email if available
    url              TEXT        NOT NULL,
    title            TEXT,
    item_type        TEXT        NOT NULL CHECK (item_type IN ('bookmark', 'homepage')),
    pattern_name     TEXT        NOT NULL,   -- which detection rule matched
    action_taken     TEXT        NOT NULL CHECK (action_taken IN ('removed', 'failed', 'skipped')),
    action_error     TEXT,                   -- populated when action_taken = 'failed'
    notified_at_utc  TIMESTAMPTZ             -- null until Google Chat DM sent
);

CREATE INDEX bookmark_violations_os_username_idx
    ON bookmark_violations (os_username, detected_at_utc DESC);

CREATE INDEX bookmark_violations_hostname_idx
    ON bookmark_violations (hostname, detected_at_utc DESC);

CREATE INDEX bookmark_violations_detected_at_idx
    ON bookmark_violations (detected_at_utc DESC);

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

GRANT USAGE ON SCHEMA public TO evidence_writer;
GRANT INSERT, SELECT ON bookmark_violations TO evidence_writer;

GRANT USAGE ON SCHEMA public TO evidence_reader;
GRANT SELECT ON bookmark_violations TO evidence_reader;

COMMIT;
