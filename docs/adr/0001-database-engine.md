# ADR 0001 — Database Engine

- **Status:** Accepted (demo scope)
- **Date:** 2026-05-21
- **Author:** Stuart Chen (Insider Threat SME)
- **Supersedes:** —
- **Related:** ADR 0002 — Evidence Store

## Context

The AI workflow demo automates an insider-threat pipeline: retrieve logs →
normalise to Elastic ECS → correlate with entity data → analyse → preserve
evidence → report.

Two distinct workloads need to be persisted:

| Workload | Characteristics |
|---|---|
| **Log store** | Append-heavy, time-series, semi-structured JSON, ECS-normalised, multi-source (Google Workspace, CrowdStrike, Mimecast Incydr, Checkpoint, DLP). High volume, long retention, full-text + field search, aggregations. |
| **Case store** | Low volume, relational, ACID. Holds cases, findings, evidence_items, evidence_custody (append-only, hash-chained per ADR 0002), audit_log. Referential integrity to log events. |

Mixing both in one engine is a common anti-pattern: forcing logs into Postgres
loses search performance and ECS-native ingest; forcing cases into
Elasticsearch / OpenSearch loses ACID and breaks the chain-of-custody
guarantees required by the project rules.

Project rules that constrain this decision:

- Normalise logs to Elastic ECS.
- Append-only chain of custody, hash-chained, signed.
- UTC timestamps; preserve original timezone in metadata.
- Pinned runtime + dependencies.
- Secrets via `.env` (local) or GCP Secret Manager (cloud); never hardcoded.
- Service accounts scoped to least privilege.
- Structured JSON logging, UTC, no secrets/PII.
- Agent auditability for every query and data access.

Engagement-specific constraints:

- Hybrid target (on-prem + GCP), but the demo runs entirely locally via
  Docker Compose.
- < 50 GB/day log volume; < 90 days hot retention for logs.
- Greenfield deployment (no existing cluster to reuse).
- Demo, not production. Cost target: zero.

## Decision

Two engines, both pinned, both run locally via `docker-compose.yml`:

| Layer | Engine | Pinned image | Role |
|---|---|---|---|
| **Log store** | **OpenSearch 2.19.4** | `opensearchproject/opensearch:2.19.4` | ECS-normalised event store; full-text + field search; aggregations |
| **Case store** | **PostgreSQL 16.4** | `postgres:16.4-alpine` | `cases`, `findings`, `evidence_items`, `evidence_custody` (append-only via trigger), `audit_log` |

Supporting decisions:

| Topic | Decision |
|---|---|
| **OpenSearch vs Elasticsearch** | OpenSearch (Apache 2.0). No license question, no SSPL ambiguity. Drop-in replaceable if Elastic-only features (ML jobs, prebuilt Elastic Security rules) are needed later. Pinned at **2.19.4** (latest 2.x patch as of 2026-05-21); 3.x deliberately deferred until pySigma backend and evidence query code are validated against the 3.x API surface. |
| **Postgres hosting** | Local Docker container, named volume for persistence, `TZ=UTC` / `PGTZ=UTC` enforced. |
| **Authentication** | Local Postgres roles (`evidence_writer`, `evidence_reader`) with SCRAM-SHA-256; OpenSearch security plugin disabled in the demo (acceptable because it binds to `127.0.0.1` only); MinIO service-account access keys per role (writer / reader / admin). All credentials in `.env` (gitignored). |
| **Schema migration** | Forward-only SQL files under `db/`, named `NNNN_*.sql`, applied automatically by the Postgres entrypoint on first boot. |
| **Backup** | None for the demo. Docker named volumes only. |
| **HA / replication** | None for the demo. Single-node Postgres, single-node OpenSearch. |
| **Monitoring** | `docker compose logs`; structured JSON logs from the application (see `src/evidence/logging_config.py`). |
| **Retention (project-wide)** | **3 months (90 days)** for all data classes in the demo: logs, cases, findings, audit_log, evidence vault. |

### Retention specifics

Retention is enforced by different mechanisms per data class:

| Data class | Mechanism | Demo value |
|---|---|---|
| Logs (OpenSearch indices) | ISM policy: roll over by age, delete after retention window | 90 days |
| Evidence vault (MinIO objects) | Object Lock GOVERNANCE retention, set per-object at upload | 90 days |
| Case rows (`cases`, `findings`) | Application-managed; no automatic deletion in demo | Policy: 90 days |
| Custody ledger (`evidence_custody`) | Application-managed; **no deletion path exists** (DB triggers block UPDATE/DELETE/TRUNCATE). Demo retains for the lifetime of the database. | Effectively immutable |
| Audit log (`audit_log`) | Application-managed; no automatic deletion in demo | Policy: 90 days |

`EVIDENCE_RETENTION_DAYS` in `.env.example` will be set to `90` and the demo
`retention_class` enum gains `demo_90d` (small code update to apply when the
coding batch resumes; tracked).

## Consequences

### Positive

- Zero-cost demo. Three containers run on a laptop.
- Engine choices align 1:1 with the project rules: ECS-native log store, ACID
  case store, append-only custody ledger.
- Both engines pinned to exact image tags; reproducible across machines.
- Cleanly graduates to the production design (Cloud SQL or on-prem Postgres
  cluster; managed OpenSearch service or self-hosted multi-node cluster) with
  no schema or application-code changes — only DSN/endpoint config.
- Bind-to-localhost + disabled OpenSearch security plugin is acceptable in
  the demo and trivially reversible in production.

### Negative / risks

- **3-month retention is short for insider-threat work.** Typical defensible
  retention for closed cases and audit records is **7 years** (employment-law
  lookback, regulatory holds, civil/criminal litigation). The 3-month value is
  a deliberate demo choice; production deployment must extend it and add a
  legal-hold class that suspends deletion.
- **Custody ledger is effectively immutable in the demo** (no deletion path),
  which is correct from a defensibility standpoint but means the demo
  database grows unboundedly across runs. Acceptable at demo volume; document
  a reset procedure (drop volume + re-run init) for engineering convenience.
- **OpenSearch security plugin disabled.** Fine for `127.0.0.1` binding;
  unacceptable for any network-exposed deployment.
- **Single-node OpenSearch and single-node Postgres.** No HA, no replication,
  no failover. A volume corruption loses everything.
- **No automated backup.** Recovery from corruption is a re-run of the demo.

### Production gaps (explicit swap-outs)

| Demo | Production |
|---|---|
| Local Docker Postgres (single node) | Cloud SQL or AlloyDB (cloud-side) **or** on-prem Postgres cluster with streaming replication + WAL archival (on-prem-side); PITR enabled |
| Local Docker OpenSearch (single node, security off) | Managed OpenSearch (cloud) or self-hosted multi-node cluster; security plugin on; TLS; SSO; node-level ILM/ISM policies |
| Local DB roles + `.env` credentials | SSO via corporate IdP for humans; workload identity / GCP service accounts for machines; secrets in GCP Secret Manager |
| MinIO Object Lock **Governance** | GCS Bucket Lock (locked) or S3 Object Lock **Compliance** |
| 90-day retention everywhere | Tiered: logs 1–2 years with ILM warm/cold/frozen; cases & audit 7 years; legal-hold class overrides; archived to immutable cold bucket |
| No backup | Daily automated backups; quarterly restore drills |
| No HA | Multi-AZ Postgres; multi-node OpenSearch with replicas |
| Plain logs to stdout | Cloud Audit Logs sinked to immutable bucket; centralised observability |

## Alternatives considered

| Option | Why rejected |
|---|---|
| **Single engine (Postgres only)** with `jsonb` for logs | Loses ECS-native ingest, search performance degrades sharply on aggregations and full-text, violates project rule on ECS normalisation. |
| **Single engine (OpenSearch only)** with documents for cases | No ACID; cannot guarantee append-only custody with referential integrity; tamper-evidence of cases would have to be rebuilt outside the DB. |
| **Elasticsearch instead of OpenSearch** | Equivalent capability at this scale, but the SSPL license adds a question we don't need to answer; no Elastic-only feature is required by the current scope. Easy to revisit. |
| **BigQuery for logs** | Cheaper at scale, but slower interactive hunting, ECS mapping is manual, and adds GCP setup the demo doesn't need. Worth revisiting at >500 GB/day. |
| **ClickHouse for logs** | Faster aggregations, cheaper at scale, but weaker full-text and no native ECS — large mapping effort for connectors. |
| **DynamoDB / Firestore for cases** | Tempting for serverless, but weak relational integrity for chain-of-custody references. Not appropriate for evidence work. |
| **SQLite + local files** | Even simpler than Docker, but no concurrency, no roles, no triggers worth trusting for append-only enforcement. Demo would not represent production patterns. |

## Confidence

**85 / 100** — engines map cleanly to the workloads; both are well-understood,
pinned, and reproducible; the demo path and the production path share the
same schema and API surface so graduation is mechanical. Gap to higher
confidence is the 3-month retention (deliberately demo-scope, explicitly
flagged) and the deferred production swap-outs (HA, SSO, managed services,
extended retention, legal hold) which need their own ADRs before any real
investigation data is collected.
