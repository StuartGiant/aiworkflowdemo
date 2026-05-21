-- AI Workflow Demo — detection schema
-- Migration: 0002_detection.sql
-- Target: PostgreSQL 16.x
-- Depends on: 0001_evidence_schema.sql
--
-- Creates:
--   * tables: detection_state, finding_evidence
--   * grants: evidence_writer INSERT/SELECT on both tables
--
-- Implements schema additions from ADR 0005 (Detection & MITRE ATT&CK Mapping).

BEGIN;

-- ---------------------------------------------------------------------------
-- detection_state
--
-- Stores the per-rule watermark used by the detection scheduler.
-- One row per Sigma rule ID; updated by the detection worker after each run.
-- ---------------------------------------------------------------------------

CREATE TABLE detection_state (
    rule_id          TEXT        PRIMARY KEY,
    rule_version     TEXT        NOT NULL,      -- git SHA of the rules/ directory at last run
    last_run_at_utc  TIMESTAMPTZ NOT NULL
);

-- ---------------------------------------------------------------------------
-- finding_evidence
--
-- Join table linking findings to the evidence_items that triggered them.
-- A finding may reference multiple evidence items (corroborated findings);
-- an evidence item may appear in multiple findings (shared artefact).
-- ---------------------------------------------------------------------------

CREATE TABLE finding_evidence (
    finding_id   UUID NOT NULL REFERENCES findings   (finding_id)   ON DELETE RESTRICT,
    artefact_id  UUID NOT NULL REFERENCES evidence_items (artefact_id) ON DELETE RESTRICT,
    PRIMARY KEY (finding_id, artefact_id)
);

CREATE INDEX finding_evidence_artefact_idx ON finding_evidence (artefact_id);

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

GRANT USAGE ON SCHEMA public TO evidence_writer;

-- evidence_writer needs to insert detection watermarks and finding↔evidence links.
GRANT INSERT, SELECT, UPDATE ON detection_state  TO evidence_writer;
GRANT INSERT, SELECT          ON finding_evidence TO evidence_writer;

-- evidence_reader: read-only on new tables.
GRANT SELECT ON detection_state, finding_evidence TO evidence_reader;

COMMIT;
