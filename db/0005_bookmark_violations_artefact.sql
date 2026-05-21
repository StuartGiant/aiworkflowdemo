-- AI Workflow Demo — link bookmark_violations to evidence_items
-- Migration: 0005_bookmark_violations_artefact.sql
-- Target: PostgreSQL 16.x
-- Depends on: 0001_evidence_schema.sql, 0004_bookmark_guard.sql
--
-- Adds evidence_artefact_id to bookmark_violations so each violation row
-- references the tamper-evident Bookmarks file snapshot taken before removal.

BEGIN;

ALTER TABLE bookmark_violations
    ADD COLUMN evidence_artefact_id UUID
        REFERENCES evidence_items(artefact_id) ON DELETE SET NULL;

CREATE INDEX bookmark_violations_artefact_idx
    ON bookmark_violations (evidence_artefact_id)
    WHERE evidence_artefact_id IS NOT NULL;

COMMIT;
