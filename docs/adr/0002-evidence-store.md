# ADR 0002 — Evidence Store (demo tier)

- **Status:** Accepted (demo scope)
- **Date:** 2026-05-21
- **Author:** Stuart Chen (Insider Threat SME)
- **Supersedes:** —
- **Related:** ADR 0001 — Database Engine (pending finalisation)

## Context

The AI workflow demo automates an insider-threat pipeline: retrieve logs → normalise to
Elastic ECS → correlate with entity data → analyse → preserve evidence → report. Every
artefact collected during an investigation must be preservable to a standard that
withstands hostile review (HR, legal, regulator, court).

Project rules that constrain this decision:

- SHA-256 every artefact at collection.
- Record chain of custody: who / when / where / how, source system, query used.
- Timestamps in UTC; preserve original timezone in metadata.
- Normalise to Elastic ECS.
- Prefer ≥ 2 independent sources; corroborate findings.
- Anti-tipping-off: the subject must never learn the investigation exists.
- Agent auditability: every query and data access is logged.
- Disposition tracking: TP / FP / inconclusive recorded.
- Severity + confidence on every finding.
- Secrets in `.env` locally, GCP Secret Manager in cloud.
- Pinned runtime + dependencies; type hints; structured JSON logging (UTC, no PII).

Constraints specific to this engagement:

- Demo, not production. Must run on a laptop or one small VM.
- Hybrid target (on-prem + GCP) was chosen in ADR 0001, but the demo stays local.
- < 50 GB/day log volume; < 90 days hot retention.
- Greenfield OpenSearch chosen for the log store; PostgreSQL 16 chosen for the case
  store.
- Evidence storage was deliberately deferred from ADR 0001 and is decided here.

## Decision

Adopt a three-component demo-tier evidence store, all runnable from a single
`docker-compose.yml`:

| Layer | Engine | Role |
|---|---|---|
| Raw artefact bytes | **MinIO** with S3 Object Lock in **Governance** mode | Write-once, read-many vault for the original byte stream as collected |
| Signed manifest | JSON file co-located in the vault, signed with a local **Ed25519** key (`pynacl`) | Cryptographically binds hash + collection metadata to the artefact |
| Chain-of-custody ledger | `evidence_custody` table in the existing PostgreSQL 16 instance, append-only via trigger, hash-chained per row | Tamper-evident record of every collection / access / transfer / disposition event |

Supporting choices:

- **ECS-normalised copy** of the artefact (where applicable) is indexed in the
  greenfield OpenSearch cluster from ADR 0001. The manifest holds the OpenSearch
  index name and document ID so the normalised copy is reachable from the
  evidence_items row but is clearly labelled as derivative — the MinIO object is the
  original.
- **Canonicalisation** for signing uses `json.dumps(sort_keys=True,
  separators=(",", ":"), ensure_ascii=False)`. This is deterministic enough for the
  demo; production should adopt full RFC 8785 JCS.
- **Audit log** is a structured JSON file (UTC, redacted, rotated). Production
  replaces this with Cloud Audit Logs sinked to an immutable bucket.
- **Roles**: separate MinIO accounts for `evidence-writer` (PUT only),
  `evidence-reader` (GET only), and `evidence-admin` (bypass governance, used only
  for the tamper demo). Postgres mirrors with `evidence_writer` (INSERT on
  custody) and `evidence_reader` (SELECT only).
- **Secrets** via `.env` (gitignored). Cloud deployment swaps in GCP Secret Manager
  with no code change (config layer reads from env vars).

## Per-artefact manifest (signed JSON)

| Field | Required | Notes |
|---|---|---|
| `artefact_id` | yes | UUIDv7 |
| `case_id` | yes | FK into `cases` |
| `source_system` | yes | e.g. `google_workspace.admin_reports`, `crowdstrike.fdr`, `mimecast.incydr`, `checkpoint.dlp` |
| `collection_method` | yes | `api` / `export` / `kql` / `spl` |
| `query` | yes | Verbatim query string, redacted of secrets |
| `collector_principal` | yes | Service account or OS user |
| `collected_at_utc` | yes | RFC 3339 |
| `original_tz` | yes | Source-system TZ |
| `bytes` | yes | Object size |
| `sha256` | yes | Hex |
| `mime_type` | yes | Detected at capture |
| `s3_uri` | yes | `s3://evidence-vault/<case>/<artefact_id>` |
| `ecs_index` | optional | OpenSearch index name |
| `ecs_doc_id` | optional | OpenSearch document id |
| `pii_tags` | yes | Array; empty if none |
| `retention_class` | yes | `demo_24h` for this scope |
| `signature_alg` | yes | `ed25519` |
| `signature` | yes | Base64 over canonicalised manifest sans `signature` |
| `signing_key_id` | yes | Fingerprint of the public key |
| `manifest_version` | yes | `1` |

## Custody ledger schema (essentials)

`evidence_custody`:

- `event_id` UUIDv7 PK
- `artefact_id` FK
- `event_type` enum: `collected | accessed | exported | transferred | disposed`
- `actor` text
- `actor_ip` inet null
- `host` text null
- `purpose` text
- `event_time_utc` timestamptz default `now() at time zone 'utc'`
- `prev_event_hash` bytea — hash of previous row for this artefact (`\\x00...` for first row)
- `this_event_hash` bytea — SHA-256 of canonicalised current row
- `signature` bytea — Ed25519 over `this_event_hash`

Trigger blocks `UPDATE` and `DELETE`. Two roles: `evidence_writer` (INSERT only),
`evidence_reader` (SELECT only). Admin / migration role is separate and audited.

## Consequences

### Positive

- Three independent integrity surfaces — Object Lock, Ed25519 signature, hash-chained
  ledger — must all be defeated to alter evidence undetected.
- All five demo components run with one `docker compose up`; cost is effectively zero.
- Code path is identical to production: the collector library calls `record_evidence`
  whether the vault is local MinIO or GCS, and whether the signing key is on-disk
  or in Cloud KMS.
- ECS normalisation and custody recording are unified behind a single API, so each
  source connector (GWS, CrowdStrike, Mimecast Incydr, Checkpoint, DLP) implements
  only the source-specific fetch.
- Project rules on SHA-256, custody, UTC, ECS, agent auditability, least privilege,
  and disposition tracking are all satisfied at demo scope.

### Negative / explicit production gaps

| Demo shortcut | Production replacement |
|---|---|
| Ed25519 software key on disk | Cloud KMS HSM-backed asymmetric key |
| No RFC 3161 trusted timestamp | External TSA per manifest |
| MinIO Object Lock **Governance** | GCS Bucket Lock (locked) or S3 Object Lock **Compliance** |
| Single region, single node | Dual-region replication, VPC Service Controls |
| Manual `verify_evidence` CLI | Scheduled weekly re-hash job + Merkle root anchoring |
| Structured-log file audit | Cloud Audit Logs sinked to immutable bucket |
| `.env` secrets | GCP Secret Manager |
| No legal-hold workflow | Separate retention class + break-glass role |
| Simple `sort_keys=True` canonicalisation | RFC 8785 JCS |

### Risks

- Governance-mode Object Lock can be bypassed by an admin role. Acceptable for the
  demo because the tamper-demo script needs to show what bypass looks like; not
  acceptable for production.
- A single Ed25519 key on disk is sufficient for demo non-repudiation but not for
  court use.
- Hybrid deployment is declared in ADR 0001 but the demo evidence vault runs
  locally; this is consistent with "demo, not production" but should be revisited
  before any real investigation data is collected.

## Alternatives considered

| Option | Why rejected for the demo |
|---|---|
| Single GCS bucket with Bucket Lock (production design) | Bucket Lock is irreversible; bad fit for a throwaway demo. Adds GCP cost and IAM setup. Worth doing in production. |
| Local filesystem with `chmod 444` / `chattr +i` | Trivially defeated by root; no API parity with production; weaker narrative. |
| Postgres `bytea` inline | Bloats the DB; no WORM semantics; weakens the "separate evidence layer" story. |
| Blockchain anchoring | Overkill at demo scale; tamper-evidence already covered by hash chain + signed manifests. |
| AWS S3 Object Lock (Compliance) | Equivalent capability to GCS Bucket Lock; would still leave the demo locally hostable, but adds AWS account setup that the project doesn't otherwise need. |

## Confidence

**80 / 100** — simple, cheap, runs locally, demonstrates every defensibility
primitive (WORM, hashing, signing, hash-chained custody, separation of duties,
tamper-evident verification), and graduates to the production design without
re-architecting. Gap to higher confidence is software-only signing and
Governance-mode Object Lock, both demo-grade by design and explicitly out of scope
for court use.
