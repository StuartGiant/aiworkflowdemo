-- AI Workflow Demo — content moderation schema
-- Migration: 0010_content_moderation.sql
-- Target: PostgreSQL 16.x
-- Depends on: 0001_evidence_schema.sql (cases, evidence_items, source_system enum)
--
-- Creates:
--   * enum: moderation_action, text_verdict_result
--   * table: moderation_decisions
--
-- Notes:
--   * moderation_decisions is append-only by convention (no UPDATE trigger needed
--     here — records are forensic logs of the moderation engine's output).
--   * REVIEW and BLOCK items also produce rows in cases + evidence_items
--     via case_writer.py; the foreign key case_id here links them.
--   * text_matched_terms is an array so the keyword filter can record every
--     term that triggered, not just a flag.

BEGIN;

-- ---------------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------------

CREATE TYPE moderation_action AS ENUM ('pass', 'review', 'block');

CREATE TYPE text_verdict_result AS ENUM (
    'pass',           -- no keyword match
    'true_positive',  -- keyword match confirmed by LLM
    'false_positive', -- keyword match overridden by LLM
    'flagged_fallback' -- keyword match, LLM unavailable
);

-- ---------------------------------------------------------------------------
-- moderation_decisions
--
-- One row per message processed by the content moderation pipeline.
-- Inserted for every verdict (PASS, REVIEW, BLOCK).
-- ---------------------------------------------------------------------------

CREATE TABLE moderation_decisions (
    decision_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Google Chat identifiers
    message_name        TEXT NOT NULL,          -- Chat API resource name, e.g. spaces/xxx/messages/yyy
    space_name          TEXT NOT NULL,          -- e.g. spaces/xxx
    sender_email        TEXT NOT NULL,          -- redacted/pseudonymous in output, stored for audit

    -- Text layer
    text_verdict        text_verdict_result NOT NULL,
    text_matched_terms  TEXT[] NOT NULL DEFAULT '{}',
    text_llm_rationale  TEXT,                   -- NULL if LLM not called or unavailable

    -- Image layer (NULL if message had no image attachment)
    image_score         SMALLINT CHECK (image_score BETWEEN 0 AND 100),
    image_verdict       moderation_action,      -- NULL if no image
    image_format        TEXT,                   -- 'jpeg' | 'bmp' | 'gif' | NULL
    image_frames_scored SMALLINT,              -- > 1 for GIFs

    -- Combined verdict
    final_action        moderation_action NOT NULL,

    -- Case linkage (NULL for PASS verdicts)
    case_id             UUID REFERENCES cases (case_id) ON DELETE RESTRICT,

    -- Audit metadata
    engine_version      TEXT NOT NULL,          -- git SHA of moderation package at run time
    llm_model           TEXT,                   -- e.g. 'claude-sonnet-4-6'
    vision_backend      TEXT NOT NULL,          -- e.g. 'cloud_vision' | 'local_model'
    processed_at_utc    TIMESTAMPTZ NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')
);

CREATE INDEX moderation_decisions_space_idx    ON moderation_decisions (space_name, processed_at_utc DESC);
CREATE INDEX moderation_decisions_sender_idx   ON moderation_decisions (sender_email, processed_at_utc DESC);
CREATE INDEX moderation_decisions_action_idx   ON moderation_decisions (final_action);
CREATE INDEX moderation_decisions_case_idx     ON moderation_decisions (case_id) WHERE case_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

GRANT USAGE ON SCHEMA public TO evidence_writer;
GRANT INSERT, SELECT ON moderation_decisions TO evidence_writer;

GRANT SELECT ON moderation_decisions TO evidence_reader;

COMMIT;
