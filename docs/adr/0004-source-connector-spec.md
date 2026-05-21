# ADR 0004 — Source Connector Spec

- **Status:** Accepted (demo scope)
- **Date:** 2026-05-21
- **Author:** Stuart Chen (Insider Threat SME)
- **Supersedes:** —
- **Related:** ADR 0001 (Database), ADR 0003 (Pipeline Architecture), ADR 0005 (Detection & MITRE)

---

## Context

The Ingest stage (ADR 0003, stage 1) needs a stable, testable interface for
pulling raw events from external log sources and writing them to OpenSearch.
Scope for this ADR is **Google Workspace only** — the sole connected source
for the current demo. The interface must, however, be defined as a Protocol
so that adding further connectors (CrowdStrike EDR, Microsoft Sentinel, DLP,
etc.) requires no changes to the orchestrator or the Ingest runner.

Log types in scope:

| Application name | What it covers | Primary insider-threat signal |
|-----------------|----------------|-------------------------------|
| `drive`  | File downloads, uploads, shares, permission changes | Exfiltration (T1567.002) |
| `login`  | Sign-in events, failed logins, suspicious-login flags | Credential misuse (T1078) |
| `admin`  | User/group/role changes, settings changes | Privilege escalation (T1098) |
| `gmail`  | Message sent/received metadata, attachment events | Data exfiltration via email (T1567) |
| `mobile` | Device enrolment, wipe, policy violations | Exfiltration via unmanaged device (T1052) |

Project rules that constrain this decision:

- Secrets never hardcoded — credentials in `.env` (local) or GCP Secret Manager (cloud).
- TLS for all external calls; timeouts and retries bounded.
- Explicit error handling; no bare except; log-and-raise.
- Structured JSON logging, UTC, no secrets/PII.
- Evidence integrity: SHA-256 every artefact at collection; chain of custody recorded.
- Agent auditability: log every query and data access.
- Input: validate and parameterise; assume hostile input.

---

## Decision

### 1 — Connector Protocol

All connectors implement the following Protocol (structural subtyping —
no base class import required, duck-typing friendly):

```python
# src/ingest/protocol.py

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class ConnectorProtocol(Protocol):
    """Structural interface every log-source connector must satisfy."""

    #: Stable identifier used as ECS `event.dataset` and in OpenSearch
    #: index names, e.g. ``"google_workspace.drive"``.
    source_system: str

    def fetch(
        self,
        *,
        start: datetime,      # UTC, inclusive
        end: datetime,        # UTC, exclusive
        run_id: uuid.UUID,
    ) -> Iterator[RawEvent]:
        """Yield raw events for the given window, one at a time.

        The caller (Ingest runner) is responsible for writing each event to
        OpenSearch and recording custody via ``evidence.record_evidence()``.
        Connectors must NOT write to any store directly.

        Raises:
            ConnectorAuthError: Unrecoverable credential / permission failure.
            ConnectorRateLimitError: Back-pressures the caller; caller retries
                after the ``retry_after`` hint (seconds).
            ConnectorTransientError: Retryable network / server error.
        """
        ...

    def health_check(self) -> HealthStatus:
        """Verify connectivity and credentials without fetching data.

        Called by ``python -m pipeline check`` before a run.
        """
        ...
```

**`RawEvent` dataclass:**

```python
# src/ingest/protocol.py (continued)

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class RawEvent:
    source_system: str          # e.g. "google_workspace.drive"
    event_id: str               # connector-native unique ID; used for dedup
    occurred_at_utc: datetime   # event timestamp, normalised to UTC
    original_timezone: str      # original TZ offset from source, e.g. "+00:00"
    raw_json: dict              # untouched payload from the API
    collected_at_utc: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        # Stable, deterministic hash of the canonical JSON representation.
        canonical = json.dumps(self.raw_json, sort_keys=True, ensure_ascii=False)
        object.__setattr__(
            self, "sha256", hashlib.sha256(canonical.encode()).hexdigest()
        )
```

**Exception hierarchy:**

```python
# src/ingest/errors.py

class ConnectorError(Exception): ...
class ConnectorAuthError(ConnectorError): ...          # permanent — dead-letter immediately
class ConnectorRateLimitError(ConnectorError):         # transient — caller backs off
    def __init__(self, msg: str, retry_after: float = 60.0) -> None:
        super().__init__(msg)
        self.retry_after = retry_after
class ConnectorTransientError(ConnectorError): ...     # transient — caller retries
```

### 2 — Google Workspace connector

**Location:** `src/ingest/connectors/google_workspace.py`

**Class:** `GoogleWorkspaceConnector`

The connector wraps the **Google Admin SDK Reports API v1**
(`activities.list`) and yields one `RawEvent` per activity record.

#### 2a — Supported applications

```python
APPLICATIONS: tuple[str, ...] = (
    "drive",
    "login",
    "admin",
    "gmail",
    "mobile",
)
```

Enabled applications are configured in `config/pipeline.yml`
(see §3 — Configuration). Only enabled applications are fetched per run.

#### 2b — Authentication

Service account with **domain-wide delegation (DWD)**. The service account
must already exist in GCP and be granted DWD in the Google Workspace Admin
console before the pipeline is run.

Required OAuth 2.0 scopes granted to the service account:

| Scope | Used by |
|-------|---------|
| `https://www.googleapis.com/auth/admin.reports.audit.readonly` | All five applications |

The connector impersonates a Workspace super-admin user via DWD so that
`activities.list` can query all users' activity (not just the service account's
own activity).

**Credential loading (no hardcoding):**

```python
# src/ingest/connectors/google_workspace.py (excerpt)

import json
import os
from google.oauth2 import service_account

SCOPES = ["https://www.googleapis.com/auth/admin.reports.audit.readonly"]

def _build_credentials() -> service_account.Credentials:
    key_path = os.environ["GOOGLE_SERVICE_ACCOUNT_KEY_PATH"]
    admin_email = os.environ["GOOGLE_WORKSPACE_ADMIN_EMAIL"]
    with open(key_path) as fh:
        key_data = json.load(fh)
    creds = service_account.Credentials.from_service_account_info(
        key_data, scopes=SCOPES
    )
    return creds.with_subject(admin_email)
```

**Required `.env` variables:**

| Variable | Description |
|----------|-------------|
| `GOOGLE_SERVICE_ACCOUNT_KEY_PATH` | Absolute path to the service-account JSON key file. The key file itself must **not** be in the repo. |
| `GOOGLE_WORKSPACE_ADMIN_EMAIL` | Email of the super-admin user the service account impersonates via DWD. |
| `GOOGLE_WORKSPACE_CUSTOMER_ID` | Google Workspace `customerId` (format: `C0xxxxxxx`) **or** the primary domain. Used as the `userKey` parameter (`all` resolves to all users for most reports). |

These three variables will be added to `.env.example` with placeholder values.

#### 2c — Fetch logic

```
for each enabled application:
    start_time = window.start (ISO 8601 UTC)
    end_time   = window.end   (ISO 8601 UTC)
    page_token = None

    loop:
        call activities.list(
            userKey       = "all",
            applicationName = application,
            customerId    = GOOGLE_WORKSPACE_CUSTOMER_ID,
            startTime     = start_time,
            endTime       = end_time,
            maxResults    = 1000,       # API max per page
            pageToken     = page_token,
        )
        log query to audit_log (stage="ingest", operation="gws.activities.list",
                                target=application, query_repr=<params_sans_secrets>)
        for each item in response["items"]:
            yield RawEvent(...)
        if no nextPageToken: break
        page_token = response["nextPageToken"]
```

Event timestamp mapping per application:

| Field in API response | Maps to `RawEvent.occurred_at_utc` |
|-----------------------|-------------------------------------|
| `items[].id.time` | Parsed as RFC 3339; converted to UTC |

`event_id` is derived from `items[].id.uniqueQualifier` (guaranteed unique by
Google for the same application + customer + time). If `uniqueQualifier` is
absent (it can be on older events), the connector falls back to
`sha256(application + id.time + id.etag)`.

#### 2d — Rate limiting

Admin SDK Reports API quota limits (as of 2026):

| Limit | Value |
|-------|-------|
| Queries per day (QPD) per application | 3,000 |
| Queries per minute (QPM) per project | 1,500 |

The connector handles quota responses explicitly:

- **HTTP 429** or **HTTP 403 with `reason: rateLimitExceeded`** →
  raises `ConnectorRateLimitError(retry_after=60.0)`.
- **HTTP 503 / connection error** → raises `ConnectorTransientError`.
- **HTTP 401 / 403 (non-quota)** → raises `ConnectorAuthError` (dead-letter
  immediately; no retry).

The Ingest runner's retry loop (from ADR 0003, §4) handles back-off. The
connector itself does not sleep.

A courtesy inter-page delay of **200 ms** is applied between pages to stay
well under the QPM limit at expected demo volumes.

#### 2e — Deduplication

The Ingest runner checks OpenSearch for an existing document with the same
`event_id` before writing. If a matching document exists, the event is
skipped and counted as a duplicate (not an error). This makes re-running the
same window idempotent.

OpenSearch document ID is set to `sha256(source_system + ":" + event_id)` so
the index always rejects true duplicates via `op_type=create` (write fails
silently on conflict).

### 3 — Configuration (`config/pipeline.yml`)

```yaml
connectors:
  google_workspace:
    enabled: true
    applications:
      - drive
      - login
      - admin
      - gmail
      - mobile
    page_size: 1000           # max items per API page (1–1000)
    inter_page_delay_ms: 200  # courtesy delay between pages
    http_timeout_seconds: 30
    max_retries: 3            # applies to transient errors only

ingest:
  batch_write_size: 500       # events buffered before bulk-write to OpenSearch
  index_prefix: "raw-events"  # final index name: raw-events-<source>-<YYYY.MM.DD>
```

### 4 — OpenSearch index naming and mapping

Raw events land in daily indices:

```
raw-events-google_workspace.drive-2026.05.21
raw-events-google_workspace.login-2026.05.21
raw-events-google_workspace.admin-2026.05.21
raw-events-google_workspace.gmail-2026.05.21
raw-events-google_workspace.mobile-2026.05.21
```

Index template (`raw-events-*`) applied at stack startup:

```json
{
  "index_patterns": ["raw-events-*"],
  "template": {
    "settings": {
      "number_of_shards": 1,
      "number_of_replicas": 0,
      "index.refresh_interval": "30s"
    },
    "mappings": {
      "dynamic": "true",
      "properties": {
        "source_system":       { "type": "keyword" },
        "event_id":            { "type": "keyword" },
        "occurred_at_utc":     { "type": "date" },
        "original_timezone":   { "type": "keyword" },
        "collected_at_utc":    { "type": "date" },
        "sha256":              { "type": "keyword", "index": false },
        "pipeline_run_id":     { "type": "keyword" },
        "raw_json":            { "type": "object", "dynamic": true }
      }
    }
  }
}
```

`pipeline_run_id` is injected by the Ingest runner (not the connector) so
every raw event can be traced back to the pipeline run that collected it.

### 5 — Evidence chain for raw events

For each batch of raw events written to OpenSearch the Ingest runner calls:

```python
evidence.record_evidence(
    artefact_id=<uuid>,
    source_system=raw_event.source_system,
    data=raw_event.raw_json,
    query=f"gws.activities.list application={app} start={start} end={end}",
    collection_method="api_pull",
    collected_by="pipeline:ingest",
    sha256=raw_event.sha256,
    run_id=run_id,
)
```

This writes an `evidence_items` row + initial `evidence_custody` entry so the
raw event is tamper-evident from the moment it is collected (ADR 0002).

### 6 — Source layout additions

```
src/
  ingest/
    __init__.py
    runner.py          # iterates connectors, writes to OpenSearch + evidence
    protocol.py        # ConnectorProtocol, RawEvent, HealthStatus
    errors.py          # ConnectorError hierarchy
    connectors/
      __init__.py
      google_workspace.py   # GoogleWorkspaceConnector
      _stub.py              # FixtureConnector for unit tests (reads NDJSON fixtures)
tests/
  ingest/
    test_google_workspace.py   # unit tests with VCR cassettes or responses mock
    fixtures/
      gws_drive_sample.ndjson
      gws_login_sample.ndjson
      gws_admin_sample.ndjson
      gws_gmail_sample.ndjson
      gws_mobile_sample.ndjson
```

---

## Consequences

### Positive

- **Single source of truth for raw events.** All connectors yield `RawEvent`;
  the Ingest runner owns writing to OpenSearch and evidence. No connector can
  accidentally bypass tamper-evidence.
- **Testable without credentials.** `_stub.py` replays fixture NDJSON in unit
  tests; the Protocol's `runtime_checkable` decorator means `isinstance`
  checks work in tests without importing the real connector.
- **Extensible without orchestrator changes.** Adding CrowdStrike, Sentinel,
  or DLP connectors requires only: (a) a new file under `connectors/`, (b) a
  new entry in `config/pipeline.yml`. The Ingest runner discovers connectors
  by config, not by hard-coded imports.
- **Idempotent re-runs.** `op_type=create` on `sha256`-keyed doc IDs means
  re-running the same window never double-writes.

### Negative / risks

- **DWD is a high-privilege pattern.** A compromised service-account key gives
  read access to all users' logs. Key file must be protected (filesystem
  permissions, gitignore, never logged). Production must rotate keys and use
  GCP Workload Identity instead of a JSON key file.
- **Gmail and Mobile logs may have lower fidelity.** Gmail activity via Admin
  SDK provides metadata only (no message bodies); Mobile provides device
  management events, not device-level file access. This is the correct
  boundary for privacy/data minimisation but limits signal depth.
- **3,000 QPD cap per application.** At 5 applications × a large tenant, a
  backfill over a wide window could exhaust the quota. Demo volumes are small;
  production must request quota increases or stagger backfills.
- **Single connector today.** Until ADR 0004 is extended for CrowdStrike/EDR
  and DLP, all findings rely on Google Workspace data only — explicitly
  single-source until corroborating connectors are added. All findings will
  carry `single_source=true` until a second connector is active.

### Production gaps

| Demo | Production |
|------|-----------|
| Service-account JSON key on disk | GCP Workload Identity (keyless); key rotation policy |
| All five GWS applications enabled | Per-investigation scope narrowing (least-privilege data access) |
| No Gmail content access | Consider Vault API for legal-hold content under proper authorisation |
| Manual fixture NDJSON for tests | VCR cassette recording from a real sandbox tenant |
| Single connector | Add CrowdStrike, Sentinel KQL, DLP, Mimecast Incydr per ADR 0004 extension |
| Courtesy 200 ms inter-page delay | Adaptive rate limiting with token-bucket per application |

---

## Alternatives considered

| Option | Why rejected |
|--------|-------------|
| **Pull via Google Chronicle / Unified Data Model** | Chronicle is a managed SIEM; would bypass OpenSearch and couple ingest to a GCP-only data plane. Not compatible with the hybrid (on-prem + cloud) target. |
| **Pub/Sub push (Google Workspace Alerts API)** | Near-real-time but requires an inbound HTTPS endpoint and GCP Pub/Sub. Complicates the local demo setup significantly; CLI pull model is simpler and sufficient for the investigation workflow. |
| **BigQuery export (GWS audit log export)** | Cheapest at scale; latency ~1 hour. Too slow for the pipeline's investigation-on-demand model; also ties ingest to GCP. |
| **Direct HTTPS via `requests`** | More control, but the `google-api-python-client` library handles OAuth refresh, retry headers, and pagination correctly. Reimplementing this is not worth the risk. |
| **Single monolithic connector (all sources, one class)** | Harder to test, impossible to enable/disable per source, violates single-responsibility. Protocol-per-connector is the correct boundary. |

---

## Required `.env.example` additions

```dotenv
# ── Google Workspace connector ────────────────────────────────────────────────
# Path to the GCP service-account JSON key file (do NOT commit the key itself).
GOOGLE_SERVICE_ACCOUNT_KEY_PATH=/path/to/service-account-key.json

# Email of the Workspace super-admin the service account impersonates via DWD.
GOOGLE_WORKSPACE_ADMIN_EMAIL=admin@yourdomain.com

# Google Workspace customer ID (format: C0xxxxxxx) or primary domain.
GOOGLE_WORKSPACE_CUSTOMER_ID=C0xxxxxxx
```

---

## Confidence

**80 / 100** — the Admin SDK Reports API is stable and well-documented; the
connector interface is minimal and covers the known extension points.
Gap to higher confidence is the single-connector limitation (all findings are
single-source until a second connector is added) and the DWD key-on-disk
pattern (acceptable for demo, must be replaced before production).
ADR 0004 should be revisited when the second connector is onboarded to
validate that the Protocol interface generalises cleanly.
