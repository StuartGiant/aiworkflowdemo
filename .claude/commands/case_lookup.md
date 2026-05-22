---
description: Look up a case record with full details — violations, evidence, custody chain, and audit log
allowed-tools: Bash
---

Look up the full details of a case in the bookmark_guard database.

The argument is a case code (e.g. `BG-Zhaos-Air-2026-05-22`) or a case UUID. If no argument is provided, show the most recent case.

Run the following steps:

1. Load the DB connection from the project `.env` file and connect as the reader role (`POSTGRES_READER_USER` / `POSTGRES_READER_PASSWORD`). Use `DYLD_LIBRARY_PATH=/opt/homebrew/opt/libpq/lib` on macOS.

2. Resolve the case using the argument:
   - No argument → most recently opened case
   - Looks like a UUID → match on `cases.case_id`
   - Looks like an email (contains `@`) → find cases where `subject_ref` matches the `os_username` in `bookmark_violations` with `chrome_email = <argument>`, then return all matching cases ordered by `opened_at_utc DESC`
   - Otherwise → try `case_code` first, then fall back to `subject_ref` (os_username) in `cases`
   - If multiple cases match (e.g. same user has several cases), display all of them in order, each with their full details.

3. Display the following sections in order:

**CASE**
All columns from the `cases` table for the matched row.

**VIOLATIONS**
All rows from `bookmark_violations` where `os_username = subject_ref`, ordered by `detected_at_utc`. Show: detected_at_utc, url, title, item_type, pattern_name, action_taken, action_error, evidence_artefact_id.

**EVIDENCE ITEMS**
All rows from `evidence_items` where `case_id` matches, ordered by `collected_at_utc`. Show: artefact_id, source_system, collected_at_utc, bytes, encode(sha256,'hex') as sha256_hex, s3_uri, pii_tags, retention_class, query.

**CUSTODY CHAIN**
All rows from `evidence_custody` where `artefact_id` in the case's evidence items, ordered by `event_time_utc`. Show: event_id, artefact_id, event_type, actor, host, purpose, event_time_utc, encode(this_event_hash,'hex') as hash.

**AUDIT LOG**
All rows from `audit_log` where `target` in the case's artefact IDs, ordered by `event_time_utc`. Show all columns.

4. If no case is found, print a clear message and list the 5 most recent cases by `opened_at_utc`.
