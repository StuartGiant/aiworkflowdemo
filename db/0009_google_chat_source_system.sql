-- AI Workflow Demo — extend source_system enum for Google Chat ingest
-- Migration: 0009_google_chat_source_system.sql
-- Target: PostgreSQL 16.x
-- Depends on: 0001_evidence_schema.sql
--
-- ALTER TYPE ... ADD VALUE cannot run inside a transaction block.

ALTER TYPE source_system ADD VALUE IF NOT EXISTS 'google_workspace.chat';

-- Sentinel case for raw ingest — evidence_items require a valid case_id, but
-- no investigation case is open at ingest time. All raw Chat artefacts are
-- parked here until the Detect stage opens a real case and re-assigns them.
INSERT INTO cases (case_id, case_code, title, subject_ref, disposition)
VALUES (
    '00000000-0000-0000-0000-000000000001',
    'INGEST-SENTINEL',
    'Raw ingest holding case — not an active investigation',
    'system',
    'open'
) ON CONFLICT (case_id) DO NOTHING;
