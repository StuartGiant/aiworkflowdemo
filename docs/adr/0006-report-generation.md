# ADR 0006 — Report Generation

- **Status:** Accepted (demo scope)
- **Date:** 2026-05-21
- **Author:** Stuart Chen (Insider Threat SME)
- **Supersedes:** —
- **Related:** ADR 0001 (Database), ADR 0002 (Evidence), ADR 0005 (Detection)

## Context

The pipeline's final stage produces an investigation report from `cases`,
`findings`, `evidence_items`, and `evidence_custody` rows. The report is the
artefact that leaves the pipeline — HR, legal, and (potentially) regulators
or law-enforcement consume it — so its integrity, reproducibility, and
redaction discipline matter at least as much as the underlying data
collection.

Project rules that constrain this decision:

- Deliverables: detailed written format with explicit evidence chains.
- Every conclusion cites evidence chain: source → event → finding.
- Severity + confidence (score + band) required on each finding.
- Single-source findings explicitly flagged.
- MITRE ATT&CK TTPs on each finding.
- UTC timestamps; preserve original TZ in metadata.
- Disposition (TP/FP/inconclusive) recorded.
- Reproducibility: rule pack version, config, model/prompt versions, seeds.
- PII data minimisation; redaction on output.
- Anti-tipping-off: subject must never see the investigation; reports must
  not leak through any subject-facing channel.
- No demographic profiling.

Engagement-specific constraints:

- Demo scope.
- User preference: "detailed written format with explicit evidence chains"
  (per `CLAUDE.md`).
- The case store, evidence store, and detection engine already exist (ADRs
  0001, 0002, 0005); the report layer should compose them, not duplicate
  them.

## Decision

A Jinja2-templated **Markdown** generator that reads from PostgreSQL and the
evidence vault, produces one `.md` file per case, hashes the rendered file,
records it as an `evidence_items` row with its own custody event, and signs
it with the same Ed25519 key used elsewhere in the pipeline.

DOCX export is **deferred** (deliberate scope reduction, see "Production
gaps").

### Components

| Component | Purpose | Location |
|---|---|---|
| **Template pack** | One root template + section partials (header, summary, timeline, findings, evidence inventory, methodology, appendix) | `templates/report/` |
| **Data assembler** | Pulls case, findings, evidence_items, custody chain from PostgreSQL; resolves rule-pack version and config hash | `src/report/assemble.py` |
| **Redactor** | Applies the per-distribution-class redaction policy to PII-tagged fields and replaces subject identity with `subject_ref` everywhere | `src/report/redact.py` |
| **Renderer** | Renders Jinja2 templates to Markdown | `src/report/render.py` |
| **Sealer** | Hashes the rendered Markdown, registers it as an `evidence_items` row, appends a `collected` custody event, signs it, writes the signature alongside the `.md` | `src/report/seal.py` |
| **CLI** | `python -m report generate --case-code <code> --distribution <class>` | `src/report/__main__.py` |

### Report structure (Markdown sections, in order)

| Section | Contents |
|---|---|
| **Header** | `case_code`, `case_id`, `subject_ref` (pseudonymous), opened/closed UTC, current disposition, overall severity, overall confidence (score + band), report version, distribution class |
| **Executive summary** | 3–6 sentences: what was investigated, what was found, current disposition, recommended action. Generated from a template, NOT free-form LLM output, unless explicitly enabled (in which case the prompt + model version are recorded in the methodology section). |
| **Timeline** | Chronological UTC table of every event that contributed to a finding. Columns: `timestamp_utc`, `original_tz`, `source_system`, `actor` (redacted per policy), `event_type`, `artefact_id` (linkable). |
| **Findings** | One section per `findings` row. Per-finding subsections: title, description, severity, confidence (score + band + per-term breakdown from ADR 0005), MITRE TTPs (linked to attack.mitre.org), corroboration status (multi-source vs **single-source** banner), evidence chain (source → event → artefact_id → SHA-256), analyst notes, disposition. |
| **Evidence inventory** | Table of every `evidence_items` row attached to the case: `artefact_id`, `source_system`, `collection_method`, `collected_at_utc`, `bytes`, `sha256` (first 16 chars + full on hover/footnote), `s3_uri`, custody-event count, retention class. |
| **Methodology & reproducibility** | Rule-pack version (git SHA), pipeline config hash, MITRE ATT&CK version, model + prompt versions if AI-assisted summarisation was used, run seed, software versions of the evidence module and detection worker, timezone policy (UTC + original TZ preserved). |
| **Appendix A — Full custody chain** | For each artefact, the output of `evidence.verify_evidence(...)` rendered as a verbatim block: every custody event with `event_type`, `actor`, `event_time_utc`, `prev_event_hash`, `this_event_hash`, signature key id, and the PASS/FAIL of the chain replay at report-generation time. |
| **Appendix B — Suppressed / single-source findings** | A separate section for findings that were below the inclusion threshold or single-source, so reviewers see what was *not* promoted to the main body. |
| **Footer** | Generation timestamp UTC, Ed25519 signature of the rendered Markdown, signing key fingerprint, report SHA-256. |

### Distribution classes

| Class | Audience | Redaction |
|---|---|---|
| `internal_ir_only` | SOC / IR / IT-Sec | Minimal. Subject identity still rendered as `subject_ref`; PII tags retained where operationally necessary. |
| `hr_handover` | HR / People Ops | PII-tagged fields redacted except those required to identify the case scope. Subject identity remains pseudonymous; the subject identity mapping is shared separately under an existing HR control. |
| `legal_counsel` | Internal legal | Same as `hr_handover` plus an explicit chain-of-custody attestation block, signed by the report sealer. |
| `external_regulator` | Regulator / law enforcement | Strictest redaction: all non-essential PII removed, demographic data never included, custody chain included in full. Requires manual analyst sign-off (CLI flag `--external-approved-by`). |

The selected class is recorded on the `evidence_items` row representing the
report (in the manifest's `pii_tags` and `extra` fields) so future readers
can see what redaction was applied.

### Report-as-evidence (sealing)

After the Markdown is rendered:

1. Compute `sha256(report_bytes)`.
2. Call `evidence.record_evidence(...)` with:
    - `case_id = <case being reported on>`
    - `source_system = 'other'`
    - `collection_method = 'manual'`
    - `query = 'report.generate(distribution=<class>, rule_pack=<sha>, config=<hash>)'`
    - `data = report_bytes`
    - `mime_type = 'text/markdown; charset=utf-8'`
    - `pii_tags = [<distribution class>, 'report']`
3. The Ed25519 signature of the report's canonical bytes is embedded in
   the report footer **and** stored on the report's `evidence_items`
   manifest (so the same signature can be verified two independent ways).
4. A `collected` custody event is appended automatically (this is what
   `record_evidence` does).

Effect: the report is itself an evidence artefact, with the same WORM /
hash-chained guarantees as any source-collected log file.

### Reproducibility metadata (footer)

```
---
Report SHA-256: <hex>
Signature (Ed25519, base64): <sig>
Signing key id: ed25519:<fingerprint>
Generated at (UTC): <iso>
Rule pack version: <git sha>
MITRE ATT&CK version: <version>
Pipeline config hash: <sha256 of merged config>
Evidence module version: <pkg version>
Detection worker version: <pkg version>
LLM-assisted sections: <list of section names or 'none'>
LLM model + prompt version (if any): <id>
Run seed: <int>
---
```

A future analyst can re-render the report by checking out the same rule
pack git SHA and config hash; differences should be limited to the
generation timestamp and signature.

### Anti-tipping-off controls

- Reports are written to a path the subject cannot reach (`./reports/`
  inside the project workspace, never to a subject-accessible share).
- The CLI refuses to render if the case's `subject_ref` resolves to an
  account that is also a configured pipeline service account (catches the
  reflexive "report about an admin account" foot-gun).
- Distribution outside the configured classes requires a separate signed
  approval step (out of scope for the demo; placeholder CLI flag exists).

### CLI surface

```
python -m report generate \
    --case-code IT-2026-014 \
    --distribution internal_ir_only \
    [--include-suppressed] \
    [--external-approved-by <name>]   # required for external_regulator
```

Stdout: the generated report path. Stderr: structured JSON log per project
rules. Non-zero exit on any failure (no partial reports written).

## Consequences

### Positive

- Reports compose the existing schema (`cases`, `findings`, `evidence_items`,
  `evidence_custody`) — no new source of truth.
- Markdown is diff-friendly in git, trivially convertible to other formats
  later, and renders in nearly every viewer your audience uses.
- Sealing the report as an `evidence_items` row means the report itself is
  tamper-evident and provable; recipients can verify the SHA-256 + signature
  independently.
- Reproducibility metadata is built-in, satisfying the project rule on
  recording seeds, config, and model/prompt versions.
- Redaction is policy-driven (distribution class), not ad-hoc per analyst.
- Anti-tipping-off and subject pseudonymity are enforced in the rendering
  layer, not relied upon downstream.

### Negative / risks

- **No DOCX export in the demo.** Hand-over to HR/legal/regulator audiences
  who require a Word document needs a manual conversion step (e.g.,
  `pandoc report.md -o report.docx`). Acceptable for the demo;
  production should add a first-class DOCX path using the project's `docx`
  skill.
- LLM-assisted summarisation is **off by default**. If enabled, the model
  version, prompt version, and exact inputs must be captured in the
  reproducibility footer or the report fails the project's reproducibility
  rule.
- Markdown does not enforce visual formatting; recipients on different
  viewers may see slightly different layouts. Acceptable for an evidence
  document; lower priority than integrity guarantees.
- 3-month case retention (ADR 0001) applies to the report's `evidence_items`
  row too. After 90 days, the report's evidence record could be archived;
  the Markdown file itself, if exported off-system, persists wherever the
  recipient stored it (outside the pipeline's control).

### Production gaps (explicit)

| Demo | Production |
|---|---|
| Markdown only | First-class DOCX export via the `docx` skill; PDF via the `pdf` skill; both with the same content and the same SHA-256 of the canonical Markdown source recorded in the DOCX/PDF metadata |
| Reports written to local `./reports/` | Reports written to a dedicated GCS bucket under Bucket Lock (compliance mode); access logged via Cloud Audit Logs |
| Single signing key, on disk | Cloud KMS HSM-backed signing for report sealing |
| Distribution class metadata + manual flag for `external_regulator` | Approval workflow with named-approver signatures; multi-party sign-off for external classes |
| LLM-assisted summarisation off by default | If enabled, model card + prompt registry; deterministic seed; cached outputs so the same case re-renders identically |
| 90-day case retention applies to reports | Reports retained for the longer of (a) case retention class and (b) any legal-hold class assigned; immutable archival to cold storage with bucket lock |
| Recipient verifies SHA-256 manually | A small verification helper (`python -m report verify <path>`) ships with releases; future: a standalone signed-verifier binary that doesn't require the full pipeline to be installed |

## Alternatives considered

| Option | Why rejected |
|---|---|
| **DOCX as the primary format** (your earlier proposal #2) | Heavier toolchain dependency; lower velocity for the demo; harder to diff in git. Worth adopting in production. |
| **PDF as the primary format** | Strong "looks final" property but worst diff/edit story; reproducibility is harder because PDF generators add nondeterministic metadata. |
| **HTML report** | Renders well, but the same content drifts visually across browsers; less natural for HR/legal handover. |
| **Wiki page (Notion / Confluence)** | Convenient for collaboration but external system controls the integrity of the artefact. Inappropriate for evidence-bearing output. |
| **Free-form LLM-generated reports** | Cannot guarantee reproducibility or that every claim cites evidence. Templates with optional, audited LLM-assist for the summary section is the defensible compromise. |
| **Single combined report for all distribution classes** | Forces the strictest redaction on everyone, hurting internal IR velocity; or forces relaxed redaction outward, breaking data minimisation. Per-class rendering is cheap and correct. |
| **In-database report rendering (Postgres functions)** | Couples report logic to the DB; hard to test, hard to evolve. Application-side rendering is standard. |

## Confidence

**80 / 100** — design composes the existing schema, satisfies every project
rule on evidence chains / severity+confidence / MITRE / reproducibility /
redaction, and ships a report-as-evidence sealing step so the report itself
is provable. Gap to higher confidence is the deferred DOCX/PDF outputs
(documented as a production gap) and the unproven LLM-assist path
(off-by-default for the demo).
