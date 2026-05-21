# ADR 0005 — Detection & MITRE ATT&CK Mapping

- **Status:** Accepted (demo scope)
- **Date:** 2026-05-21
- **Author:** Stuart Chen (Insider Threat SME)
- **Supersedes:** —
- **Related:** ADR 0001 (Database), ADR 0002 (Evidence), ADR 0003 (Pipeline — pending), ADR 0004 (Connectors — pending), ADR 0006 (Reports)

## Context

The pipeline must turn ECS-normalised events in OpenSearch into actionable
`findings` rows in PostgreSQL, with severity, confidence (score + band),
MITRE ATT&CK TTPs, corroboration status, and an evidence chain. The detection
layer is the link between raw telemetry and the case record.

Project rules that constrain this decision:

- Findings cite an evidence chain: source → event → finding.
- Severity (`low/medium/high/critical`) and confidence (1–100, bucketed into
  the four bands `weak/mixed/strong/multi_source`) are **both** required.
- Confidence bands per the project rule:
    - 1–39 weak/conflicting
    - 40–69 mixed/unknowns
    - 70–89 strong, minor gaps
    - 90–100 multiple strong sources agree
- Prefer ≥ 2 independent sources; explicitly flag single-source findings.
- Map each finding to MITRE ATT&CK TTPs.
- Disposition (TP/FP/inconclusive) recorded for tuning.
- Agent auditability: every query and data access logged.
- Reproducibility: rule pack version, config, seeds in run metadata.

Engagement-specific constraints:

- Demo scope: must run locally without a managed SIEM.
- OpenSearch 2.15 is the event store (ADR 0001).
- Greenfield — no existing detection corpus to migrate.

## Decision

A Python detection worker that loads **Sigma YAML rules** with insider-threat
front-matter extensions, converts them to OpenSearch query DSL via the
`pysigma-backend-opensearch` library, executes on a 5-minute schedule, and
writes structured `findings` rows to PostgreSQL with full evidence chain
references.

### Components

| Component | Purpose | Location |
|---|---|---|
| **Rule pack** | Versioned directory of Sigma YAML rules with insider-threat front matter | `rules/` (git, lockfile = git SHA) |
| **Rule loader** | Parses YAML, validates schema, resolves front-matter extensions | `src/detect/rules.py` |
| **Compiler** | Converts Sigma → OpenSearch query DSL via pySigma backend | `src/detect/compile.py` |
| **Scheduler** | Runs all enabled rules on a 5-minute tumbling window, with `last_run_at` watermarks per rule | `src/detect/scheduler.py` |
| **Engine** | Executes the compiled query, paginates results, normalises hits | `src/detect/engine.py` |
| **Correlator** | For each hit, waits ≤ `corroboration_window` for matching event from a different `source_system` | `src/detect/correlate.py` |
| **Finding writer** | Builds `findings` row + links to `evidence_items` for each constituent event; uses the existing `evidence.record_evidence()` API for the raw OpenSearch documents that triggered the rule | `src/detect/findings.py` |
| **Tuning report** | Nightly aggregator of disposition (TP/FP/inconclusive) per rule | `scripts/tuning_report.py` |

### Rule format

Standard Sigma plus one custom block `insider_threat:`:

```yaml
title: Bulk Drive download by non-privileged user
id: IT-0001
status: experimental
description: >
  More than 200 Drive file downloads in 10 minutes by a user not in the
  approved bulk-export list.
author: stuart.chen
date: 2026-05-21
logsource:
  product: google_workspace
  service: drive
detection:
  selection:
    eventName: download
  timeframe: 10m
  condition: selection | count() by actor > 200
fields:
  - actor
  - actor_ip
  - object_id
falsepositives:
  - Approved bulk-export operators (see allowlist)
level: high                       # baseline severity
tags:
  - attack.exfiltration
  - attack.t1567.002              # exfiltration to cloud storage

# --- insider-threat extension (custom front-matter) -------------------
insider_threat:
  base_confidence: 55             # 0-100
  corroboration:
    required: true
    window_minutes: 15
    other_sources:
      - dlp.checkpoint
      - mimecast.incydr
  severity_bumps:
    - condition: actor_in_privileged_group
      delta: 1                    # high -> critical
  asset_critical_bonus: 10
  privileged_actor_bonus: 10
  corroboration_bonus: 30
  single_source_penalty: -10
  allowlist:
    actor:
      - bulk-export-svc@example.com
```

Rules without the `insider_threat:` block fall back to defaults documented in
`rules/SCHEMA.md`.

### Severity assignment

1. Start from `level:` in Sigma (`low | medium | high | critical`).
2. Apply `severity_bumps` deltas in order; clamp to `[low, critical]`.
3. Final severity is written to `findings.severity` (ENUM in DB).

### Confidence scoring (formula, auditable)

```
score = base_confidence
      + (corroboration_bonus  if corroborated   else single_source_penalty)
      + (privileged_actor_bonus if actor_privileged else 0)
      + (asset_critical_bonus   if asset_critical   else 0)
score = clamp(score, 1, 100)
band  = weak        if score <= 39
      = mixed       if score <= 69
      = strong      if score <= 89
      = multi_source otherwise
```

Both the score and the per-term contributions are logged to `audit_log` so an
analyst (or auditor) can reconstruct why a given finding landed at its
confidence value.

### Corroboration

For every primary hit:

1. Compute a correlation key (default: `actor` + 15-minute window centred on
   the event).
2. Query OpenSearch for events matching the rule's
   `corroboration.other_sources` within the window, with the same correlation
   key.
3. If ≥ 1 match → `findings.single_source = false`, apply
   `corroboration_bonus`.
4. If 0 matches and `corroboration.required = true` → still create the
   finding (do not suppress), but with `single_source = true` and
   `single_source_penalty` applied. The report renderer (ADR 0006) calls this
   out visually.

This satisfies the project rule "prefer ≥ 2 independent sources; explicitly
flag single-source findings" without dropping evidence on the floor.

### MITRE ATT&CK mapping

- Extracted from Sigma `tags:` entries matching `attack.t\d+(\.\d+)?`.
- Normalised to uppercase MITRE IDs (`T1567.002`).
- Stored verbatim in `findings.mitre_ttps TEXT[]` (already in DB schema per
  migration `0001_evidence_schema.sql`).
- The MITRE version (e.g., `ATT&CK v15`) used by the rule pack is recorded in
  the rule-pack lockfile so reports can cite it.

### Evidence chain

For each constituent event of a finding:

1. Worker fetches the raw OpenSearch document(s) via doc id.
2. Calls `evidence.record_evidence(...)` with the raw JSON as `data`, the
   rule id as part of `query`, and `source_system` set to the matched
   `logsource`. This produces an `evidence_items` row + custody event.
3. Inserts a join row in a new table `finding_evidence (finding_id,
   artefact_id)` linking the finding to each piece of evidence.

### Scheduling

- 5-minute tumbling window. Each rule has its own `last_run_at_utc`
  watermark stored in a `detection_state` table.
- Lookback is `max(rule.timeframe, 5m) + corroboration_window` to avoid edge
  misses.
- Re-running the same rule with the same watermark is idempotent
  (deterministic `finding_id` derived from `sha256(rule_id || window_start ||
  correlation_key)`), so a crash-restart will not double-write findings.

### Tuning loop

- Analyst sets `findings.disposition` (TP/FP/inconclusive) via the case UI
  or a CLI.
- Nightly `scripts/tuning_report.py` aggregates per-rule precision:
  `TP / (TP + FP)` for closed findings over a rolling 30-day window.
- No automatic weight changes — analyst-driven only. Avoids feedback-loop
  drift; keeps the human in the loop.

### Schema additions (small)

To support this ADR, the next migration (`db/0002_detection.sql`) adds:

```sql
CREATE TABLE detection_state (
    rule_id          TEXT PRIMARY KEY,
    rule_version     TEXT NOT NULL,
    last_run_at_utc  TIMESTAMPTZ NOT NULL
);

CREATE TABLE finding_evidence (
    finding_id  UUID NOT NULL REFERENCES findings(finding_id)        ON DELETE RESTRICT,
    artefact_id UUID NOT NULL REFERENCES evidence_items(artefact_id) ON DELETE RESTRICT,
    PRIMARY KEY (finding_id, artefact_id)
);
```

## Consequences

### Positive

- Detection logic is **portable**: Sigma rules can be re-targeted to Sentinel
  (KQL) or Elastic if/when the engine changes.
- Confidence scoring is a **closed, auditable formula** — every finding's
  score can be reconstructed from the rule version + the constituent events.
- Corroboration is **enforced in the engine**, not left to the analyst.
- Evidence chain is **automatic** — every constituent event goes through
  `evidence.record_evidence()`, so the integrity guarantees from ADR 0002
  apply without per-rule effort.
- Idempotent scheduling tolerates restarts; no double-writes.

### Negative / risks

- pySigma OpenSearch backend support varies across Sigma constructs; some
  exotic rules (e.g., aggregation correlations) may need hand-rolled DSL.
- 5-minute cadence means worst-case detection latency is ~5 minutes plus the
  corroboration window. Acceptable for insider threat; not acceptable for
  high-velocity attack types (out of scope here).
- Analyst-driven tuning means precision drift is possible if dispositions
  aren't kept current.

### Production gaps

| Demo | Production |
|---|---|
| Single Python worker on a 5-minute schedule | Highly-available scheduler (e.g., Kubernetes CronJob with leader election) |
| Sigma + insider-threat front matter, hand-curated | Add UEBA baselining (per-user/per-asset behavioural baselines) and asset-criticality lookups |
| No automatic tuning | Optional ML-assisted suggestions surfaced to the analyst (never auto-applied) |
| Asset-critical / privileged-actor lookups from static YAML | Identity graph (AD / Workspace directory / HR system) join |
| MITRE version recorded in lockfile | MITRE Navigator export of coverage; gap analysis |
| Detection runs against a single OpenSearch instance | Cross-cluster search for federated detection |

## Alternatives considered

| Option | Why rejected |
|---|---|
| **Custom YAML DSL** | Simpler short-term, but loses Sigma's community rule corpus and portability across SIEMs. Not worth the lock-in. |
| **OpenSearch Alerting plugin** | Keeps rules close to the data, lower ops cost. Rejected because rules become harder to unit-test, harder to version-control alongside application code, and harder to port off OpenSearch. |
| **Pure KQL in Sentinel** | Best-in-class for cloud SIEM, but binds detection to a vendor and isolates it from the on-prem path. Hybrid was already chosen in ADR 0001. |
| **ML-only anomaly detection** | Black-box scoring is hard to defend in evidence reports. Rule-based with optional ML augmentation is the defensible default. |
| **Stream processing (Flink / Kafka Streams)** | Overkill at < 50 GB/day; large operational surface for a demo. |
| **Auto-tuning rule weights** | Risk of silent precision drift; loses analyst control of severity/confidence. Tuning report is offered, automatic adjustment is not. |

## Confidence

**78 / 100** — the architecture is well-understood (Sigma + Python worker is
a standard pattern), the scoring formula is auditable, corroboration is
enforced rather than recommended, and evidence chains piggyback on the
ADR 0002 evidence module. Gap to higher confidence is pySigma backend
coverage (some rules may need hand-rolled DSL) and the absence of a true
UEBA baseline (deliberately deferred to production).
