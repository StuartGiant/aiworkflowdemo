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
- **Evidence preservation** — Before any modification, the raw `Bookmarks` file is snapshotted into the evidence vault (MinIO) with a signed manifest and chain-of-custody record. The artefact ID is linked to the violation row.
- **Response** — Matching bookmarks and homepages are atomically removed from Chrome's profile files.
- **Violation record** — Each removal is written to the `bookmark_violations` table in PostgreSQL, including the URL, pattern matched, action taken, and evidence artefact ID.
- **Notification** — An email is sent to the employee via Gmail (Google Workspace Domain-Wide Delegation) informing them that sensitive bookmarks were detected and removed.

**Chrome extension (sync-safe enforcement):**

Because Chrome Sync can restore removed bookmarks when Chrome reconnects to Google's servers, a companion Manifest V3 Chrome extension provides real-time enforcement inside Chrome itself. It uses `chrome.bookmarks.remove()` — a Chrome-internal call — so the deletion is propagated through sync to all the user's devices. The extension runs on startup and on every new bookmark creation.

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

> Chrome must be closed before a live run. If Chrome is open, it will overwrite file changes on next sync. The Chrome extension handles the real-time / sync-safe layer.

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

---

## Requirements

- macOS 26.0+
- Python 3.12+ (use `venv/`)
- libpq (`brew install libpq`)
- Docker Desktop
- Google Workspace service account with Domain-Wide Delegation
  - Scopes: `admin.reports.audit.readonly`, `gmail.send`
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
