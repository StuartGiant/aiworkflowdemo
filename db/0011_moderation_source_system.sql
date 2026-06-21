-- AI Workflow Demo — extend source_system enum for content moderation
-- Migration: 0011_moderation_source_system.sql
-- Target: PostgreSQL 16.x
-- Depends on: 0001_evidence_schema.sql (source_system enum)
--
-- Adds 'google_workspace.chat_moderation' to source_system so that
-- evidence_items rows created by the content moderation pipeline can be
-- distinguished from raw ingest rows from the Google Chat connector.

BEGIN;

ALTER TYPE source_system ADD VALUE IF NOT EXISTS 'google_workspace.chat_moderation';

COMMIT;
