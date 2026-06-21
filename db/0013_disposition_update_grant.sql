-- AI Workflow Demo — grant disposition update to evidence_writer
-- Migration: 0013_disposition_update_grant.sql
-- Depends on: 0001_evidence_schema.sql
--
-- Allows the content moderation pipeline (evidence_writer role) to update the
-- disposition column on cases when a reviewer clicks a button in a Chat card.

BEGIN;

GRANT UPDATE (disposition) ON cases TO evidence_writer;

COMMIT;
