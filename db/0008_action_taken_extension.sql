-- AI Workflow Demo — extend action_taken check constraint for extension removal
-- Migration: 0008_action_taken_extension.sql
-- Target: PostgreSQL 16.x
-- Depends on: 0004_bookmark_guard.sql

BEGIN;

ALTER TABLE bookmark_violations
    DROP CONSTRAINT bookmark_violations_action_taken_check;

ALTER TABLE bookmark_violations
    ADD CONSTRAINT bookmark_violations_action_taken_check
        CHECK (action_taken = ANY (ARRAY[
            'removed'::text,
            'removed_by_extension'::text,
            'failed'::text,
            'skipped'::text
        ]));

COMMIT;
