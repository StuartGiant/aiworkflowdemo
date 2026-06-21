-- AI Workflow Demo — add sender_email to cases
-- Migration: 0012_cases_sender_email.sql
-- Target: PostgreSQL 16.x
-- Depends on: 0001_evidence_schema.sql, 0007_evidence_writer_cases_insert.sql
--
-- Adds an explicit sender_email column to cases for querying by actor email.
-- subject_ref retains its pseudonymous role; sender_email stores the raw address.

BEGIN;

ALTER TABLE cases ADD COLUMN sender_email TEXT;

CREATE INDEX cases_sender_email_idx ON cases (sender_email);

COMMIT;
