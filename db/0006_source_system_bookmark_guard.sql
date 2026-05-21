-- AI Workflow Demo — extend source_system enum for bookmark_guard
-- Migration: 0006_source_system_bookmark_guard.sql
-- Target: PostgreSQL 16.x
-- Depends on: 0001_evidence_schema.sql
--
-- ALTER TYPE ... ADD VALUE cannot run inside a transaction block.

ALTER TYPE source_system ADD VALUE IF NOT EXISTS 'bookmark_guard.chrome';
