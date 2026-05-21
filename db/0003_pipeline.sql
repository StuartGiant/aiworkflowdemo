-- AI Workflow Demo — pipeline state schema
-- Migration: 0003_pipeline.sql
-- Target: PostgreSQL 16.x
-- Depends on: 0001_evidence_schema.sql
--
-- Creates:
--   * tables: pipeline_runs, pipeline_errors, entity_hits
--   * indexes: stage lookup, entity lookup
--   * grants: evidence_writer INSERT/SELECT/UPDATE on pipeline_runs;
--             INSERT/SELECT on pipeline_errors and entity_hits
--
-- Implements schema additions from ADR 0003 (Pipeline Architecture).

BEGIN;

-- ---------------------------------------------------------------------------
-- pipeline_runs
--
-- One row per stage per CLI invocation. Groups of rows sharing the same
-- pipeline_run_id represent one end-to-end pipeline execution.
-- The orchestrator writes a 'running' row before touching any data and
-- updates it to 'completed' or 'dead_lettered' when the stage finishes.
-- ---------------------------------------------------------------------------

CREATE TABLE pipeline_runs (
    run_id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_run_id  UUID         NOT NULL,   -- groups all stages of one invocation
    stage            TEXT         NOT NULL
                                  CHECK (stage IN
                                      ('ingest','normalise','correlate',
                                       'detect','evidence','report')),
    status           TEXT         NOT NULL
                                  CHECK (status IN
                                      ('running','completed','failed','dead_lettered')),
    watermark_start  TIMESTAMPTZ  NOT NULL,
    watermark_end    TIMESTAMPTZ  NOT NULL,
    started_at_utc   TIMESTAMPTZ  NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
    completed_at_utc TIMESTAMPTZ,
    records_in       INTEGER,
    records_out      INTEGER,
    config_hash      TEXT         NOT NULL,   -- SHA-256 of config/pipeline.yml at run time
    error_message    TEXT,

    UNIQUE (pipeline_run_id, stage)
);

CREATE INDEX pipeline_runs_pipeline_run_id_idx
    ON pipeline_runs (pipeline_run_id, stage);

CREATE INDEX pipeline_runs_status_idx
    ON pipeline_runs (status, started_at_utc DESC);

-- ---------------------------------------------------------------------------
-- pipeline_errors
--
-- One row per failed attempt of a batch item (after retries are exhausted).
-- The orchestrator advances the watermark past the failed batch and continues;
-- these rows are the analyst's queue for manual review.
-- ---------------------------------------------------------------------------

CREATE TABLE pipeline_errors (
    error_id        UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          UUID         NOT NULL REFERENCES pipeline_runs (run_id),
    stage           TEXT         NOT NULL,
    attempt         INTEGER      NOT NULL CHECK (attempt >= 1),
    error_class     TEXT         NOT NULL,   -- Python exception class name
    error_message   TEXT         NOT NULL,
    traceback       TEXT,
    payload_sha256  TEXT,                    -- SHA-256 of the failed record / batch key
    created_at_utc  TIMESTAMPTZ  NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')
);

CREATE INDEX pipeline_errors_run_id_idx
    ON pipeline_errors (run_id);

CREATE INDEX pipeline_errors_created_at_idx
    ON pipeline_errors (created_at_utc DESC);

-- ---------------------------------------------------------------------------
-- entity_hits
--
-- Written by the Correlate stage: records which entity (user / asset / group)
-- matched each ECS event, and whether that entity is privileged or critical.
-- Consumed by the Detect stage to apply confidence bonuses per ADR 0005.
-- ---------------------------------------------------------------------------

CREATE TABLE entity_hits (
    hit_id               UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id               UUID         NOT NULL REFERENCES pipeline_runs (run_id),
    event_id             TEXT         NOT NULL,   -- OpenSearch _id of the ECS event
    source_system        TEXT         NOT NULL,
    entity_type          TEXT         NOT NULL
                                      CHECK (entity_type IN ('user','asset','group')),
    entity_id            TEXT         NOT NULL,   -- value from entities/ YAML fixture
    is_privileged        BOOLEAN      NOT NULL DEFAULT FALSE,
    is_asset_critical    BOOLEAN      NOT NULL DEFAULT FALSE,
    matched_at_utc       TIMESTAMPTZ  NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')
);

CREATE INDEX entity_hits_event_id_idx
    ON entity_hits (event_id);

CREATE INDEX entity_hits_entity_id_idx
    ON entity_hits (entity_id);

CREATE INDEX entity_hits_run_id_idx
    ON entity_hits (run_id);

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

GRANT USAGE ON SCHEMA public TO evidence_writer;

-- Orchestrator (evidence_writer role) needs to create and update run rows.
GRANT INSERT, SELECT, UPDATE ON pipeline_runs   TO evidence_writer;
GRANT INSERT, SELECT          ON pipeline_errors TO evidence_writer;
GRANT INSERT, SELECT          ON entity_hits     TO evidence_writer;

-- evidence_reader: read-only.
GRANT SELECT ON pipeline_runs, pipeline_errors, entity_hits TO evidence_reader;

COMMIT;
