# ADR 0003 — Pipeline Architecture

- **Status:** Accepted (demo scope)
- **Date:** 2026-05-21
- **Author:** Stuart Chen (Insider Threat SME)
- **Supersedes:** —
- **Related:** ADR 0001 (Database), ADR 0002 (Evidence Store), ADR 0004 (Source Connectors — pending), ADR 0005 (Detection & MITRE), ADR 0006 (Reports)

---

## Context

The AI workflow demo automates an end-to-end insider-threat investigation pipeline:

```
ingest → normalise → correlate → detect → evidence → report
```

Each stage has distinct I/O characteristics, failure modes, and auditability
requirements. The architecture must satisfy:

- **Agent auditability** — every query and data-access operation is logged.
- **Evidence integrity** — partial runs must not produce un-hashed or
  un-chained evidence rows (see ADR 0002).
- **Reproducibility** — a run can be re-executed from any watermark with
  the same config and produce the same findings.
- **Explicit error handling** — no bare-except; failed items are preserved,
  not silently dropped.
- **Structured logging** — JSON, UTC, no secrets or PII.

Engagement-specific constraints:

- Demo runs locally; zero additional infrastructure beyond the existing
  Docker Compose stack (Postgres, OpenSearch, MinIO).
- Trigger model: **manual / CLI** — the pipeline is invoked on demand by
  the analyst, not by a daemon. This matches the investigative workflow
  (Stuart runs the pipeline against a time window when investigating a
  subject, not continuously).
- < 50 GB/day log volume; latency tolerance measured in minutes, not seconds.

---

## Decision

### 1 — Stage model

Six discrete stages, each a Python module under `src/`:

| # | Stage | Module | Input | Output |
|---|-------|--------|-------|--------|
| 1 | **Ingest** | `src/ingest/` | Connector config + time window | Raw events in OpenSearch (`raw-events-*` indices) |
| 2 | **Normalise** | `src/normalise/` | Raw events (OpenSearch) | ECS-normalised events in OpenSearch (`ecs-events-*` indices) |
| 3 | **Correlate** | `src/correlate/` | ECS events + entity YAML fixtures | Correlation annotations written back to event documents; `entity_hits` table in Postgres |
| 4 | **Detect** | `src/detect/` | ECS + correlated events (OpenSearch) | `findings` rows in Postgres + `evidence_items` rows via `evidence.record_evidence()` |
| 5 | **Evidence** | `src/evidence/` *(existing)* | Evidence items collected by Detect | Artefacts sealed in MinIO; `evidence_custody` chain extended |
| 6 | **Report** | `src/report/` | `cases`, `findings`, `evidence_items` (Postgres) | Markdown report; report itself sealed as an `evidence_items` row |

Stages 4 and 5 share the existing `src/evidence/` module; Detect calls
`record_evidence()` directly and the evidence module handles sealing.

### 2 — Orchestrator

A thin orchestrator at `src/pipeline/orchestrator.py` drives the stages in
order. It is invoked via the CLI entry point:

```
python -m pipeline run [OPTIONS]

Options:
  --stages TEXT     Comma-separated list of stages to run (default: all)
                    Values: ingest,normalise,correlate,detect,evidence,report
  --from DATETIME   Override watermark start (ISO 8601 UTC). Default: last
                    successful watermark from pipeline_runs.
  --to   DATETIME   Override watermark end (ISO 8601 UTC). Default: now().
  --case-id UUID    Attach this run to an existing case (optional).
  --dry-run         Execute queries but write nothing to Postgres or MinIO.
  --config PATH     Path to pipeline config YAML (default: config/pipeline.yml).
```

The orchestrator:

1. Resolves the time window (from/to) and locks a `pipeline_runs` row with
   `status='running'` before touching any data.
2. Calls each enabled stage in order, passing the run context.
3. On completion, updates `pipeline_runs.status` to `'completed'` and
   records `records_in`, `records_out`, and `completed_at_utc`.
4. On unrecoverable failure (after retries — see §4), sets
   `pipeline_runs.status` to `'dead_lettered'`.
5. Logs a `audit_log` entry for every external query (OpenSearch searches,
   Postgres writes, MinIO PUTs) with the stage name, query string or
   operation, and a SHA-256 of the request payload where applicable.

### 3 — Stage hand-off via PostgreSQL state table

Stages communicate exclusively through the database — never via in-process
shared state or temp files. This keeps each stage independently re-runnable
and the full pipeline auditable.

**`pipeline_runs` table** (one row per stage per invocation):

```sql
CREATE TABLE pipeline_runs (
    run_id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_run_id  UUID         NOT NULL,          -- groups all stages of one CLI invocation
    stage            TEXT         NOT NULL
                                  CHECK (stage IN
                                    ('ingest','normalise','correlate',
                                     'detect','evidence','report')),
    status           TEXT         NOT NULL
                                  CHECK (status IN
                                    ('running','completed','failed','dead_lettered')),
    watermark_start  TIMESTAMPTZ  NOT NULL,
    watermark_end    TIMESTAMPTZ  NOT NULL,
    started_at_utc   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    completed_at_utc TIMESTAMPTZ,
    records_in       INTEGER,
    records_out      INTEGER,
    config_hash      TEXT         NOT NULL,  -- SHA-256 of pipeline config YAML
    error_message    TEXT,
    UNIQUE (pipeline_run_id, stage)
);
```

**`pipeline_errors` table** (one row per failed attempt of a batch item):

```sql
CREATE TABLE pipeline_errors (
    error_id        UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          UUID         NOT NULL REFERENCES pipeline_runs(run_id),
    stage           TEXT         NOT NULL,
    attempt         INTEGER      NOT NULL,  -- 1-indexed
    error_class     TEXT         NOT NULL,
    error_message   TEXT         NOT NULL,
    traceback       TEXT,
    payload_sha256  TEXT,        -- SHA-256 of the failed record / batch key
    created_at_utc  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
```

The orchestrator selects the most recent `completed` row for each stage as
its "last known good" watermark when no `--from` override is given.

**`entity_hits` table** (written by the Correlate stage):

```sql
CREATE TABLE entity_hits (
    hit_id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          UUID         NOT NULL REFERENCES pipeline_runs(run_id),
    event_id        TEXT         NOT NULL,  -- OpenSearch _id
    source_system   TEXT         NOT NULL,
    entity_type     TEXT         NOT NULL CHECK (entity_type IN ('user','asset','group')),
    entity_id       TEXT         NOT NULL,  -- value from YAML fixture
    is_privileged   BOOLEAN      NOT NULL DEFAULT FALSE,
    is_asset_critical BOOLEAN    NOT NULL DEFAULT FALSE,
    matched_at_utc  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
```

### 4 — Failure handling: retry with exponential back-off then dead-letter

Each stage processes events in batches. Per-batch failure policy:

```
attempt 1  → immediate
attempt 2  → back-off 1 s
attempt 3  → back-off 4 s
attempt 4  → back-off 16 s
exhausted  → write to pipeline_errors (status='dead_lettered');
             advance watermark past the failed batch;
             continue pipeline from next batch
```

Rules:

- Transient errors (connection reset, timeout, HTTP 429/503) are retried.
- Permanent errors (schema validation failure, hash mismatch, missing
  required field) are dead-lettered immediately without retrying.
- A dead-lettered item is logged to `pipeline_errors` with `payload_sha256`
  so the analyst can identify and re-submit the affected records.
- The overall `pipeline_runs` row stays `'completed'` even if some items
  are dead-lettered, so the watermark advances and the next run doesn't
  re-process the same window. Dead-letter count is surfaced in `records_out`
  as a negative sentinel (or a separate `records_dead_lettered` column —
  left to implementation).
- If the orchestrator itself fails (e.g., process killed), the `pipeline_runs`
  row remains `'running'`. On next invocation the orchestrator detects this
  stale row and marks it `'failed'` before starting a fresh run.

### 5 — Entity data: static YAML fixtures

For the demo, correlation draws from versioned YAML files under `entities/`:

```
entities/
  users.yml          # known user accounts, display names, department, manager
  assets.yml         # critical assets (servers, data stores, code repos)
  privileged.yml     # groups / roles whose members get privileged_actor_bonus
  allowlists.yml     # per-rule actor allowlists (referenced by Sigma rules)
  SCHEMA.md          # field definitions and validation rules
```

The Correlate stage loads these files at startup, validates them against
`entities/SCHEMA.md`, and resolves each ECS event's `user.name` /
`host.name` / `source.ip` against the fixtures. Results are written to
`entity_hits`.

Entity data is **not** fetched live from Active Directory or Google Workspace
Admin SDK in the demo. That swap-out is noted as a production gap below.

### 6 — Source layout

```
src/
  pipeline/
    __init__.py
    orchestrator.py      # CLI entry point + stage sequencer
    context.py           # RunContext dataclass (run_id, watermark, config)
    retry.py             # exponential back-off decorator + dead-letter writer
    audit.py             # thin wrapper: every external query → audit_log row
  ingest/
    __init__.py
    runner.py            # calls connector adapters (ADR 0004), writes raw events
  normalise/
    __init__.py
    runner.py            # ECS mapper per source type
    mappers/             # one file per source (google_workspace.py, crowdstrike.py, …)
  correlate/
    __init__.py
    runner.py            # loads entity YAML, annotates events, writes entity_hits
    loader.py            # YAML fixture reader + validator
  detect/                # ADR 0005
    …
  evidence/              # existing (ADR 0002)
    …
  report/                # ADR 0006
    __init__.py
    runner.py
    templates/           # Jinja2 templates
config/
  pipeline.yml           # connector refs, stage enable flags, batch sizes, timeouts
entities/
  users.yml
  assets.yml
  privileged.yml
  allowlists.yml
  SCHEMA.md
```

### 7 — Auditability

Every external operation is wrapped by `pipeline.audit.log_query()`:

```python
def log_query(
    *,
    stage: str,
    operation: str,          # e.g. "opensearch.search", "postgres.insert", "minio.put"
    target: str,             # index name, table name, or bucket/key
    query_repr: str,         # serialised query / statement (no PII, no secrets)
    payload_sha256: str | None = None,
    run_id: uuid.UUID,
) -> None: ...
```

Rows land in `audit_log` (existing table from ADR 0002) with
`actor='pipeline:<stage>'`. This satisfies the project rule:
*"Agent auditability: log every query and data access this workflow performs."*

---

## Consequences

### Positive

- **Zero new infrastructure.** The pipeline runs as a single Python process
  against the existing three-container stack; no scheduler service, no
  message broker, no additional ports.
- **Fully re-runnable from any watermark.** `--from` and `--to` flags allow
  targeted re-investigation of any time window without disturbing later runs.
- **Auditable by design.** Every external I/O goes through `audit.log_query()`;
  every stage transition is a Postgres row. A forensic auditor can reconstruct
  exactly what queries were run and when.
- **Evidence integrity preserved.** Detect calls `evidence.record_evidence()`
  directly; the Correlate stage only writes to `entity_hits` and annotations,
  never bypassing the custody chain.
- **Dead-letter preserves partial progress.** A single bad batch doesn't halt
  the pipeline; the error is recorded for review and the watermark advances.

### Negative / risks

- **CLI-only means no continuous monitoring.** An analyst must remember to run
  the pipeline. Missed runs leave a detection gap. Acceptable for demo;
  must be addressed before any production use (see Production gaps).
- **In-process retry means a process crash loses retry state.** After a crash,
  the stale `pipeline_runs` row is detected and the entire stage window is
  retried from scratch on next invocation. This is safe (idempotent writes) but
  means duplicate work.
- **Static entity YAML drifts from reality.** Privileged-group membership
  changes in AD will not be reflected until the YAML is manually updated.
  Single-source entity data is the correct flag level for any finding that
  depends on it.
- **No parallelism within a stage.** Stages process batches sequentially.
  At < 50 GB/day this is acceptable; above that threshold, a worker pool
  or streaming approach would be needed.

### Production gaps

| Demo | Production |
|------|-----------|
| Manual CLI trigger | Kubernetes CronJob or Cloud Scheduler with leader-election lock |
| Single Python process | Each stage as an independent service; inter-stage queue (Pub/Sub or Kafka) for back-pressure |
| Static YAML entity fixtures | Live AD / Google Workspace directory API + HR system feed; incremental sync |
| In-process exponential back-off | Dead-letter queue (Cloud Tasks / SQS); dedicated retry worker |
| `pipeline_runs` watermark in local Postgres | Distributed watermark store (Firestore / Redis) for multi-node deployment |
| `--dry-run` flag | Full shadow-mode with separate shadow indices and shadow case DB |
| No pipeline monitoring UI | Ops dashboard on `pipeline_runs` + dead-letter alert to SOC Slack channel |

---

## Schema additions

This ADR requires a new migration `db/0003_pipeline.sql`:

```sql
-- pipeline_runs: one row per stage per CLI invocation
CREATE TABLE pipeline_runs (
    run_id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_run_id  UUID         NOT NULL,
    stage            TEXT         NOT NULL
                                  CHECK (stage IN
                                    ('ingest','normalise','correlate',
                                     'detect','evidence','report')),
    status           TEXT         NOT NULL
                                  CHECK (status IN
                                    ('running','completed','failed','dead_lettered')),
    watermark_start  TIMESTAMPTZ  NOT NULL,
    watermark_end    TIMESTAMPTZ  NOT NULL,
    started_at_utc   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    completed_at_utc TIMESTAMPTZ,
    records_in       INTEGER,
    records_out      INTEGER,
    config_hash      TEXT         NOT NULL,
    error_message    TEXT,
    UNIQUE (pipeline_run_id, stage)
);

-- pipeline_errors: dead-lettered items for analyst review
CREATE TABLE pipeline_errors (
    error_id        UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          UUID         NOT NULL REFERENCES pipeline_runs(run_id),
    stage           TEXT         NOT NULL,
    attempt         INTEGER      NOT NULL,
    error_class     TEXT         NOT NULL,
    error_message   TEXT         NOT NULL,
    traceback       TEXT,
    payload_sha256  TEXT,
    created_at_utc  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- entity_hits: correlation output (Correlate stage → Detect stage)
CREATE TABLE entity_hits (
    hit_id             UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id             UUID         NOT NULL REFERENCES pipeline_runs(run_id),
    event_id           TEXT         NOT NULL,
    source_system      TEXT         NOT NULL,
    entity_type        TEXT         NOT NULL
                                    CHECK (entity_type IN ('user','asset','group')),
    entity_id          TEXT         NOT NULL,
    is_privileged      BOOLEAN      NOT NULL DEFAULT FALSE,
    is_asset_critical  BOOLEAN      NOT NULL DEFAULT FALSE,
    matched_at_utc     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_entity_hits_event_id  ON entity_hits (event_id);
CREATE INDEX idx_entity_hits_entity_id ON entity_hits (entity_id);
CREATE INDEX idx_pipeline_runs_stage   ON pipeline_runs (pipeline_run_id, stage);
```

---

## Alternatives considered

| Option | Why rejected |
|--------|-------------|
| **Direct function calls (single in-process chain)** | No per-stage audit trail; a crash loses all intermediate state; watermark cannot be reset to mid-pipeline without re-running earlier stages. |
| **File-based hand-off (NDJSON temp files)** | Not audit-friendly; cleanup is fragile; files are not first-class database citizens and cannot be queried alongside case data. |
| **Workflow engine (Prefect / Airflow)** | Adds a scheduler service, a metadata DB, and a UI server to the local stack — significant overhead for a demo. The value (DAG visualisation, retries, sensors) accrues at production scale. Deferred to the production path. |
| **Event-driven trigger (webhook / file watch)** | Requires an inbound HTTP listener or inotify daemon in the container. Complicates the demo (port exposure, auth for the webhook). The investigative workflow is inherently pull-based: the analyst decides when to run. |
| **Stream processing (Flink / Kafka Streams)** | Overkill at < 50 GB/day. Adds four or more new services to the stack. Revisit above 500 GB/day or when sub-minute detection latency is required. |
| **Celery / RQ task queue** | Requires Redis or RabbitMQ broker. Adds a service; complicates the local stack; no advantage over a simple retry loop at demo volume. |

---

## Confidence

**82 / 100** — the architecture is deliberately simple (one process, one CLI
command, Postgres as the state backbone) and every design choice traces to a
project rule. The Postgres-as-state-table pattern is well-understood and
keeps the demo fully auditable with no new infrastructure. Gap to higher
confidence is the CLI-only trigger (no continuous monitoring, explicit
production gap) and the static entity YAML (privileged-group drift is a
real detection risk, explicitly flagged as a production swap-out). ADR 0004
(source connectors) will fill the remaining blank in the Ingest stage.
