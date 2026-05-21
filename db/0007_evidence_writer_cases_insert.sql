-- AI Workflow Demo — grant evidence_writer INSERT on cases
-- Migration: 0007_evidence_writer_cases_insert.sql
-- Target: PostgreSQL 16.x
-- Depends on: 0001_evidence_schema.sql
--
-- bookmark_guard auto-creates a daily case (BG-<host>-<date>) before
-- preserving evidence.  evidence_writer needs INSERT on cases for this.

BEGIN;

GRANT INSERT ON TABLE cases TO evidence_writer;

COMMIT;
