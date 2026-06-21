# AI Workflow Demo

Automated insider threat pipeline built for the Cybersecurity team. Demonstrates how to use AI tooling to automate log retrieval, entity correlation, threat analysis, evidence preservation, and enforcement actions.

## Project overview

The pipeline detects and responds to insider threat indicators without manual analyst intervention. Each automation module is self-contained: it detects, responds, records evidence, and notifies — producing a traceable evidence chain from raw signal to remediation.

---

## Automations

### 1. Bookmark Guard

Detects and removes Chrome bookmarks and homepages that contain sensitive URLs (PII endpoints, internal financial systems, health records, bulk data exports, streaming services, etc.).

**How it works:**

- **Detection** — Reads Chrome `Bookmarks` and `Preferences` files for every Chrome profile on the host. Only corporate profiles (`@zeroinsiderai.com`) are scanned; personal profiles are skipped entirely.
- **Chrome force-close** — If Chrome is running when the script fires, it is gracefully quit (AppleScript). If Chrome does not close within 5 seconds it is hard-killed (`pkill -9`). This ensures the Bookmarks file is not locked during modification.
- **Evidence preservation** — Before any modification, the raw `Bookmarks` file is snapshotted into the evidence vault (MinIO) with a signed manifest and chain-of-custody record. The artefact ID is linked to the violation row.
- **Response** — Matching bookmarks and homepages are atomically removed from Chrome's profile files (`.tmp` → `os.replace`). Chrome's bookmark checksum is cleared so it recalculates silently on next launch.
- **Violation record** — Each removal is written to the `bookmark_violations` table in PostgreSQL, including the URL, pattern matched, action taken, and evidence artefact ID.
- **Notification** — An email is sent to the employee via Gmail (Google Workspace Domain-Wide Delegation) informing them that sensitive bookmarks were detected and removed.
- **Chrome restart** — If Chrome was running when the script started, it is relaunched with `--load-extension` pointing at the companion extension. This triggers `onInstalled` → `scanAll()` inside Chrome, catching any bookmarks Chrome Sync restores after startup. A second scan fires 5 seconds later via the `sync_check` alarm to handle Sync race conditions.

**Chrome extension (sync-safe enforcement):**

A companion Manifest V3 Chrome extension handles Sync-restored bookmarks inside Chrome itself. It uses `chrome.bookmarks.remove()` — a Chrome-internal call — so the deletion is treated as a user action and propagated through Sync to all the user's devices.

The extension is loaded via `--load-extension` each time the Python responder relaunches Chrome after a remediation run. It fires on two triggers:
- `onInstalled` — immediate scan when the extension loads
- `sync_check` alarm — re-scan 5 seconds after load to catch Sync-restored bookmarks

The `onCreated` real-time listener is intentionally disabled; enforcement only runs at startup.

For enterprise rollout the extension is deployed as a force-installed managed extension via Google Workspace Admin Console (users cannot remove it).

**Components:**

| Path | Description |
|------|-------------|
| `src/automation/bookmark_guard/` | Python automation package |
| `src/automation/bookmark_guard/detector.py` | Scans Chrome profiles, returns `ScanResult` |
| `src/automation/bookmark_guard/responder.py` | Removes bookmarks/homepages, preserves evidence, writes violations |
| `src/automation/bookmark_guard/notifier.py` | Sends Gmail notification via service account DWD |
| `src/automation/bookmark_guard/config.py` | Config loader (YAML + env vars) |
| `src/automation/bookmark_guard/models.py` | `BookmarkMatch`, `ScanResult`, `RemovalOutcome` dataclasses |
| `src/chrome_extension/bookmark_guard/` | Chrome extension (Manifest V3) |
| `config/bookmark_guard.yml` | Detection patterns and notification settings |
| `scripts/run_bookmark_guard.py` | CLI entry point (`--dry-run`, `--verbose`) |
| `db/0004_bookmark_guard.sql` | `bookmark_violations` table |
| `db/0005_bookmark_violations_artefact.sql` | Adds `evidence_artefact_id` FK to violations |
| `db/0006_source_system_bookmark_guard.sql` | Adds `bookmark_guard.chrome` to `source_system` enum |
| `db/0007_evidence_writer_cases_insert.sql` | Grants `evidence_writer` INSERT on `cases` |
| `db/0008_action_taken_extension.sql` | Adds `removed_by_extension` to `action_taken` enum |

**Run (dry run — no changes):**
```bash
source venv/bin/activate
DYLD_LIBRARY_PATH=/opt/homebrew/opt/libpq/lib \
python scripts/run_bookmark_guard.py --config config/bookmark_guard.yml --dry-run
```

**Run (live):**
```bash
source venv/bin/activate
DYLD_LIBRARY_PATH=/opt/homebrew/opt/libpq/lib \
python scripts/run_bookmark_guard.py --config config/bookmark_guard.yml
```

> Chrome does not need to be closed before running — the responder force-closes it automatically if it is open, then relaunches it with the companion extension loaded after remediation is complete.

**Detection patterns** (defined in `config/bookmark_guard.yml`):

| Pattern | What it matches |
|---------|----------------|
| `pii_endpoint` | PII data endpoint paths |
| `ssn_in_url` | SSN format in URL |
| `credit_card_in_url` | Credit card number format in URL |
| `internal_hr` | Internal HR system hostnames |
| `payroll_system` | Internal payroll/compensation hostnames |
| `internal_finance` | Internal finance/accounting/treasury hostnames |
| `classified_docs` | Confidential/classified/restricted document stores |
| `admin_user_portal` | Admin portals with user/personnel access |
| `bulk_data_export` | Bulk export URLs (CSV, XLSX, JSON, Parquet) |
| `health_records` | EHR/EMR/HIPAA health record system hostnames |
| `netflix` | Netflix and all subdomains |

---

### 2. Google Chat Ingest

Pulls Google Chat message history from the domain into OpenSearch for threat hunting and insider threat correlation. Covers two source sets:

- **Spaces** — all named spaces and group chats in the domain (SPACE + GROUP_CHAT types)
- **DMs** — direct-message threads for a configured list of PoI (Person of Interest) subjects; the service account DWD-impersonates each PoI to discover their DM spaces

For DM messages, the recipient is resolved at collection time via `spaces.members.list` and embedded in the raw document as `_space_members`, so both parties are available for entity correlation without a separate join.

**How it works:**

- **Watermark** — Each run reads the `watermark_end` of the most recent completed `pipeline_runs` row to fetch only new messages. First run defaults to 24 hours ago. Override with `--start` / `--end`.
- **Fetch** — Calls Google Chat API v1 (`spaces.list`, `spaces.members.list`, `spaces.messages.list`) via service account with Domain-Wide Delegation.
- **Write** — Events are bulk-written to OpenSearch in batches of 500. Duplicate messages (re-run of the same window) are silently skipped via `op_type=create`.
- **Pipeline state** — Each run writes a `pipeline_runs` row (status, watermark, record counts). Connector failures write to `pipeline_errors` for analyst review.

**Storage:**

| Store | What is written |
|-------|----------------|
| OpenSearch | Raw Chat message documents (full JSON per message) |
| PostgreSQL `pipeline_runs` | Watermark, status, records in/out per run |
| PostgreSQL `pipeline_errors` | Dead-lettered items on connector failure |

Evidence artefacts (MinIO) are **not** written by this ingest — Chat messages are raw operational logs, not forensic artefacts. Evidence preservation applies at the detection and response stages.

**OpenSearch index pattern:**
```
raw-events-google_workspace_chat-YYYY.MM.DD
```

**Document fields (per message):**

| Field | Source |
|-------|--------|
| `event_id` | `message.name` (e.g. `spaces/XYZ/messages/ABC`) |
| `occurred_at_utc` | `message.createTime` |
| `source_system` | `google_workspace.chat` |
| `sender` | `message.sender` (email, displayName, type) |
| `text` | `message.text` (plaintext body) |
| `attachment` | `message.attachment[]` (file metadata, Drive IDs) |
| `_space_members` | DM only — both parties' email and displayName |
| `sha256` | SHA-256 of canonical `raw_json` |

**Components:**

| Path | Description |
|------|-------------|
| `src/ingest/protocol.py` | `ConnectorProtocol`, `RawEvent`, `HealthStatus` (ADR 0004) |
| `src/ingest/errors.py` | `ConnectorError` hierarchy |
| `src/ingest/connectors/google_chat.py` | `GoogleChatConnector` — spaces, DM discovery, member fetch, message pagination |
| `src/ingest/runner.py` | Watermark resolution, OpenSearch bulk write, `pipeline_runs` management |
| `config/pipeline.yml` | Connector settings, batch size, OpenSearch connection |
| `scripts/run_ingest.py` | CLI entry point (`--start`, `--end`, `--dry-run`, `--verbose`) |
| `stuart_tests/validate_google_chat_dwd.py` | Validates DWD scopes and API connectivity |
| `db/0009_google_chat_source_system.sql` | Adds `google_workspace.chat` to `source_system` enum; inserts ingest sentinel case |

**DWD scopes required** (add all five to the service account in Google Workspace Admin > Security > API Controls > Domain-wide Delegation):
```
https://www.googleapis.com/auth/chat.spaces.readonly
https://www.googleapis.com/auth/chat.memberships.readonly
https://www.googleapis.com/auth/chat.messages.readonly
https://www.googleapis.com/auth/chat.admin.spaces.readonly
https://www.googleapis.com/auth/chat.admin.memberships.readonly
```

**Validate credentials:**
```bash
source venv/bin/activate
DYLD_LIBRARY_PATH=/opt/homebrew/opt/libpq/lib \
python stuart_tests/validate_google_chat_dwd.py
```

**Run (dry run — fetch and log, no writes):**
```bash
source venv/bin/activate
DYLD_LIBRARY_PATH=/opt/homebrew/opt/libpq/lib \
python scripts/run_ingest.py --dry-run --verbose
```

**Run (live):**
```bash
source venv/bin/activate
DYLD_LIBRARY_PATH=/opt/homebrew/opt/libpq/lib \
python scripts/run_ingest.py
```

**Run a specific time window:**
```bash
python scripts/run_ingest.py --start 2026-06-20T00:00:00Z --end 2026-06-20T23:59:59Z
```

**Add PoI subjects for DM ingestion** — edit `config/pipeline.yml`:
```yaml
ingest:
  connectors:
    google_chat:
      dms:
        poi_emails:
          - jane.doe@zeroinsiderai.com
```

**Query messages in OpenSearch:**
```bash
curl -s "http://localhost:9200/raw-events-google_workspace_chat-*/_count"
curl -s "http://localhost:9200/raw-events-google_workspace_chat-*/_search?pretty&size=5"
```

---

### 3. Content Moderation

Real-time content moderation for Google Chat messages. Screens text and image attachments within ~1–2 seconds of posting, blocks or routes flagged content to human review, and provides a reviewer workflow for case disposition.

**How it works:**

- **Pub/Sub listener** — A Workspace Events API subscription delivers message-created events to a Pub/Sub topic. The listener polls continuously and processes each message through the text and image layers.
- **Auto-reactivation** — Workspace Events subscriptions expire if not reactivated. The listener calls the reactivate API every 5 minutes automatically.
- **Sender resolution** — Sender identity is resolved from the Chat user resource name to a real email address via the Admin Directory API (DWD). Results are cached per listener session.

**Text moderation — two-tier keyword routing:**

- **Hard-block tier** (`src/moderation/text/keywords/hard_block.txt`, ~36 terms) — unambiguous terms (fullz, CC dumps, RDP for sale, CSAM, etc.) that auto-BLOCK with no LLM call. Zero false-positive risk.
- **Soft-flag tier** (`ldnoobw.txt` + `tech_ecommerce_extension.txt`, ~532 terms) — context-dependent terms routed to the LLM screener. `claude-haiku-4-5` determines TRUE_POSITIVE vs FALSE_POSITIVE. If the Anthropic API is unavailable the keyword result stands (fail-safe, not fail-open).

**Image moderation:**

Google Cloud Vision SafeSearch scores images (JPEG, BMP, GIF) for violence on a 0–100 scale. GIFs are frame-sampled at 1 fps (max 30 frames); the worst frame score determines the verdict.

| SafeSearch likelihood | Score | Action |
|---|---|---|
| VERY_UNLIKELY / UNLIKELY | 10 / 30 | Pass |
| POSSIBLE | 60 | Review |
| LIKELY / VERY_LIKELY | 80 / 95 | Block |

**Decision routing:**

| Text result | Image score | Final action |
|---|---|---|
| Pass | 0–50 | Pass — no action |
| Soft-flag (TRUE_POSITIVE) or — | 51–70 | Human review |
| Hard-block or soft-flag (TRUE_POSITIVE) or — | 71–100 | Block |
| Either layer is Block | — | Block overrides Review |

**Actions on BLOCK:**
1. Original message replaced with tombstone: `🚫 This content has been removed by the Security Content Moderation system.` — text replaced and attachments cleared via `spaces.messages.patch` (DWD credentials)
2. Case created in `cases` table; text/image evidence preserved in `evidence_items`
3. Reviewer notified via Google Chat DM (interactive card with disposition buttons) and email

**Actions on REVIEW:**
1. Message is NOT deleted — reviewer must see the content
2. Case created in `cases` table; evidence preserved
3. Reviewer notified via Google Chat DM (interactive card with disposition buttons) and email

**Reviewer disposition workflow:**

The reviewer DM is a `cardsV2` card with three link buttons: **✅ True Positive**, **❌ False Positive**, **❓ Inconclusive**. Clicking a button opens a browser tab pointing at the interaction server (`/disposition?case_id=...&disposition=...`), which:
- Updates `cases.disposition` in the DB
- If disposition is `true_positive` on a REVIEW case, tombstones the original message (same patch as BLOCK)
- Returns a confirmation page that auto-closes in 3 seconds

**Interaction server:**

A Flask HTTP server runs in a background thread on port 8080 (configurable in `config/content_moderation.yml`). It must be exposed publicly via ngrok or a stable HTTPS URL and the public URL set in `INTERACTION_BASE_URL`. The ngrok URL changes on each restart — update `.env` and restart the listener when it does.

**Credentials split:**

| Credential type | Scopes | Used for |
|---|---|---|
| DWD (impersonating admin) | `chat.messages`, `chat.spaces.readonly` | Message patch (tombstone), media download |
| Bot (service account, no DWD) | `chat.bot` | Sending DM cards, `findDirectMessage`, `spaces.setup` |
| DWD (admin) | `admin.directory.user.readonly` | Resolving sender email from user resource name |

**Image moderation phases (see ADR 0007):**

| Phase | Backend | Status |
|-------|---------|--------|
| 1 | Google Cloud Vision SafeSearch | Current |
| 2 | Local fine-tuned EfficientNet-B3 (privacy-first) | Future — when ≥ 5,000 labelled images accumulate |

**Components:**

| Path | Description |
|------|-------------|
| `src/moderation/models.py` | `ContentItem`, `TextVerdict`, `ImageVerdict`, `ModerationDecision` dataclasses |
| `src/moderation/config.py` | Typed config loader (YAML + env vars); `InteractionServerConfig` |
| `src/moderation/orchestrator.py` | Combines text + image verdicts, triggers actions, `--dry-run` support |
| `src/moderation/chat_listener.py` | Pub/Sub subscriber; decodes Chat Events, fetches attachments, auto-reactivates subscription |
| `src/moderation/text/keyword_filter.py` | Two-tier keyword scan returning `KeywordScanResult` (hard_block_terms, soft_flag_terms) |
| `src/moderation/text/llm_screener.py` | Anthropic `claude-haiku-4-5` semantic screener |
| `src/moderation/text/moderator.py` | Hard-block → auto-BLOCK; soft-flag → LLM; no match → PASS |
| `src/moderation/text/keywords/hard_block.txt` | ~36 unambiguous hard-block terms (no LLM call) |
| `src/moderation/text/keywords/ldnoobw.txt` | Base soft-flag list (MIT-licensed, ~400 terms) |
| `src/moderation/text/keywords/tech_ecommerce_extension.txt` | Tech/e-comm soft-flag extension (~130 terms) |
| `src/moderation/image/protocol.py` | `ImageScorerBackend` pluggable protocol |
| `src/moderation/image/violence_detector.py` | `VisionAPIBackend` (Phase 1) + `LocalModelBackend` stub (Phase 2) |
| `src/moderation/image/moderator.py` | Maps score → PASS / REVIEW / BLOCK |
| `src/moderation/actions/card_builder.py` | Builds `cardsV2` alert cards and resolved-disposition cards |
| `src/moderation/actions/chat_responder.py` | Tombstones blocked messages; sends card DMs to reviewer |
| `src/moderation/actions/email_notifier.py` | Sends alert email via Gmail API (DWD) |
| `src/moderation/actions/case_writer.py` | Creates `cases` + `evidence_items` records |
| `src/moderation/actions/interaction_handler.py` | Flask app: `/disposition` GET (button link handler), `/chat/interactions` POST (Chat callback fallback) |
| `config/content_moderation.yml` | Keyword lists, LLM model, image backend, Pub/Sub, interaction server port |
| `scripts/run_content_moderation.py` | CLI entry point; starts Pub/Sub listener + Flask interaction server in thread |
| `db/0010_content_moderation.sql` | `moderation_decisions` table + `moderation_action` / `text_verdict_result` enums |
| `db/0011_moderation_source_system.sql` | Adds `google_workspace.chat_moderation` to `source_system` enum |
| `db/0012_cases_sender_email.sql` | Adds `sender_email` column to `cases` |
| `db/0013_disposition_update_grant.sql` | Grants `UPDATE (disposition)` on `cases` to `evidence_writer` |
| `docs/adr/0007-image-moderation-backend.md` | ADR: two-phase image backend strategy |
| `tests/moderation/` | Unit + integration tests |

**Required DWD scopes** (add to service account in Google Workspace Admin → Security → API Controls → Domain-wide Delegation):
```
https://www.googleapis.com/auth/chat.messages
https://www.googleapis.com/auth/chat.spaces.readonly
https://www.googleapis.com/auth/chat.memberships.readonly
https://www.googleapis.com/auth/admin.directory.user.readonly
https://www.googleapis.com/auth/gmail.send
```

**Required env vars** (add to `.env`):
```
ANTHROPIC_API_KEY=sk-ant-...
MODERATION_REVIEWER_EMAIL=stuart.chen@zeroinsiderai.com
MODERATION_REVIEWER_CHAT_USER_ID=users/<numeric_id>
PUBSUB_PROJECT_ID=ai-workflow-demo-496914
PUBSUB_SUBSCRIPTION_ID=chat-events-sub
WORKSPACE_EVENTS_SUBSCRIPTION_NAME=subscriptions/<subscription_name>
INTERACTION_BASE_URL=https://<ngrok-id>.ngrok-free.app
```

**GCP setup required (one-time):**
1. Enable: Google Chat API, Cloud Vision API, Cloud Pub/Sub API, Workspace Events API, Admin SDK API
2. Create a Pub/Sub topic and subscription (`chat-events-sub`)
3. Create a Workspace Events subscription for the target Chat space pointing at the Pub/Sub topic
4. Expose port 8080 via ngrok: `ngrok http 8080`; set `INTERACTION_BASE_URL` to the ngrok HTTPS URL

**Run:**
```bash
source venv/bin/activate
python scripts/run_content_moderation.py

# Detection only — no deletions, no DB writes, no notifications
python scripts/run_content_moderation.py --dry-run
```

**Switch to local model (Phase 2, when ready):**
```yaml
# config/content_moderation.yml
image_moderation:
  backend: local_model
```

No other code changes required.

---

## Evidence data model

Each automation run that results in a removal produces a fully linked evidence chain:

```
cases
 └── evidence_items  (artefact snapshot + signed manifest)
      └── evidence_custody  (chain-of-custody events: collected → accessed → …)
 └── bookmark_violations  (violation rows, each FK'd to an evidence_item)
audit_log  (append-only record of every record_evidence / verify_evidence call)
```

**Case auto-creation:** bookmark_guard creates a daily case keyed `BG-<hostname>-<YYYY-MM-DD>` on first violation of the day. Subsequent runs on the same host/day reuse the same case, so all artefacts for a day are grouped under one case record.

**Chain-of-custody events** are hash-chained: each event records `prev_event_hash` and `this_event_hash` (SHA-256), plus an Ed25519 signature. Deletes and updates are blocked at the database trigger level — the chain is append-only and tamper-evident.

**Querying a case end-to-end:**
```sql
-- All evidence for a case
SELECT artefact_id, collected_at_utc, bytes, encode(sha256,'hex'), s3_uri
FROM evidence_items WHERE case_id = '<case_id>';

-- Custody chain for an artefact
SELECT event_type, actor, purpose, event_time_utc,
       encode(prev_event_hash,'hex'), encode(this_event_hash,'hex')
FROM evidence_custody WHERE artefact_id = '<artefact_id>' ORDER BY event_time_utc;

-- Violations linked to a case
SELECT detected_at_utc, chrome_email, url, pattern_name, action_taken, evidence_artefact_id
FROM bookmark_violations
WHERE evidence_artefact_id IN (
    SELECT artefact_id FROM evidence_items WHERE case_id = '<case_id>'
);

-- Audit trail for a case's artefacts
SELECT event_time_utc, actor, action, outcome, details
FROM audit_log
WHERE target IN (SELECT artefact_id::text FROM evidence_items WHERE case_id = '<case_id>');
```

---

## Evidence module (`src/evidence/`)

Shared evidence preservation library used by all automations. Provides two public functions:

- `record_evidence(...)` — Computes SHA-256, uploads raw bytes to the MinIO vault (Object Lock), builds and Ed25519-signs a manifest, writes `evidence_items` and custody chain rows to PostgreSQL.
- `verify_evidence(...)` — Re-downloads the artefact, re-hashes, verifies the manifest signature, replays the custody chain. Returns a structured `VerificationReport`.

Signing uses Ed25519 keys (PEM-encoded PKCS#8 / SPKI or raw 32-byte). Production swap-in is a Cloud KMS / HSM-backed key; the `sign` / `verify` interface is unchanged.

---

## Infrastructure

Local development stack runs in Docker (`docker/docker-compose.yml`):

| Container | Service | Port |
|-----------|---------|------|
| `aiwf-postgres` | PostgreSQL 16 | 5432 |
| `aiwf-minio` | MinIO (S3-compatible object store) | 9000 / 9001 |
| `aiwf-opensearch` | OpenSearch | 9200 |

**Start the stack:**
```bash
docker compose -f docker/docker-compose.yml up -d
```

**Database roles:**

| Role | Privileges |
|------|-----------|
| `aiwf_admin` | Full access (migrations, schema changes) |
| `evidence_writer` | INSERT / SELECT on evidence tables and `cases` |
| `evidence_reader` | SELECT only |

---

## Database migrations

Apply in order. Each script is idempotent-safe when re-run on a clean DB:

| File | Description |
|------|-------------|
| `0001_evidence_schema.sql` | Core schema: `cases`, `evidence_items`, `custody_chain`, `audit_log` |
| `0001b_set_role_passwords.sh` | Sets DB role passwords from env |
| `0002_detection.sql` | Detection pipeline tables |
| `0003_pipeline.sql` | Pipeline run tracking |
| `0004_bookmark_guard.sql` | `bookmark_violations` table |
| `0005_bookmark_violations_artefact.sql` | `evidence_artefact_id` FK on violations |
| `0006_source_system_bookmark_guard.sql` | Extends `source_system` enum |
| `0007_evidence_writer_cases_insert.sql` | Grants INSERT on `cases` to evidence_writer |
| `0008_action_taken_extension.sql` | Adds `removed_by_extension` to `action_taken` enum |
| `0009_google_chat_source_system.sql` | Adds `google_workspace.chat` to `source_system` enum; inserts ingest sentinel case |
| `0010_content_moderation.sql` | `moderation_decisions` table; `moderation_action` + `text_verdict_result` enums |
| `0011_moderation_source_system.sql` | Adds `google_workspace.chat_moderation` to `source_system` enum |
| `0012_cases_sender_email.sql` | Adds `sender_email` column to `cases` |
| `0013_disposition_update_grant.sql` | Grants `UPDATE (disposition)` on `cases` to `evidence_writer` |

---

## Requirements

- macOS 26.0+
- Python 3.12+ (use `venv/`)
- libpq (`brew install libpq`)
- Docker Desktop
- Google Workspace service account with Domain-Wide Delegation
  - Bookmark Guard scopes: `admin.reports.audit.readonly`, `gmail.send`
  - Google Chat ingest scopes: `chat.spaces.readonly`, `chat.memberships.readonly`, `chat.messages.readonly`, `chat.admin.spaces.readonly`, `chat.admin.memberships.readonly`
- Ed25519 signing keypair (see `keys/`)

Install Python dependencies:
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Copy and populate environment variables:
```bash
cp .env.example .env
# Edit .env with your Postgres, MinIO, and Google credentials
```
