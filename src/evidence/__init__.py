"""Evidence module — public API.

Two functions cover the entire collector / verifier surface:

    record_evidence(...)   -- called by every source connector when it has
                              fetched an artefact. Computes hash, uploads to
                              the vault, signs the manifest, records cases /
                              evidence_items / custody rows, audits the run.

    verify_evidence(...)   -- called on read. Re-downloads the artefact,
                              re-hashes it, verifies the manifest signature,
                              and replays the custody chain. Returns a
                              VerificationReport with a PASS/FAIL outcome and
                              a per-step trace.

Both functions raise EvidenceError subclasses on failure; ``verify_evidence``
returns a structured report even on failure so callers can render it.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import psycopg

from .config import EvidenceConfig
from .custody import append_event, replay_chain, CustodyEvent
from .db import reader_conn, writer_conn
from .errors import (
    ConfigError,
    CustodyError,
    EvidenceError,
    IntegrityError,
    SignatureError,
    StorageError,
    ValidationError,
)
from .logging_config import get_logger
from .manifest import Manifest, sha256_hex, utc_now_iso
from .signing import sign, signing_config_keys, verify
from .storage import (
    artefact_key,
    get_artefact,
    get_manifest,
    manifest_key,
    put_artefact,
    put_manifest,
)

__all__ = [
    "EvidenceConfig",
    "EvidenceError",
    "ConfigError",
    "CustodyError",
    "IntegrityError",
    "SignatureError",
    "StorageError",
    "ValidationError",
    "RecordResult",
    "VerificationReport",
    "record_evidence",
    "verify_evidence",
]

log = get_logger("evidence")


# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------

@dataclass(slots=True, frozen=True)
class RecordResult:
    artefact_id: str
    manifest: Manifest
    s3_uri: str
    manifest_uri: str
    custody_event: CustodyEvent


@dataclass(slots=True)
class VerificationReport:
    artefact_id: str
    ok: bool
    steps: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def add(self, step: str, ok: bool, **detail: Any) -> None:
        self.steps.append({"step": step, "ok": ok, **detail})


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def _audit(
    cfg: EvidenceConfig,
    *,
    actor: str,
    action: str,
    target: str | None,
    outcome: str,
    **details: Any,
) -> None:
    """Best-effort audit insert. Logged-and-raised on DB failure ONLY if the
    primary action also failed; otherwise we log the audit miss and continue
    so a degraded audit pipeline doesn't drop evidence."""
    try:
        with writer_conn(cfg.postgres) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_log (actor, action, target, outcome, details)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                """,
                (actor, action, target, outcome, json.dumps(details)),
            )
            conn.commit()
    except psycopg.Error as exc:
        log.error(
            "audit insert failed",
            extra={"context": {"action": action, "target": target, "err": str(exc)}},
        )


# ---------------------------------------------------------------------------
# record_evidence
# ---------------------------------------------------------------------------

def record_evidence(
    cfg: EvidenceConfig,
    *,
    case_id: str,
    source_system: str,
    collection_method: str,
    query: str,
    collector_principal: str,
    original_tz: str,
    data: bytes,
    mime_type: str,
    pii_tags: list[str] | None = None,
    ecs_index: str | None = None,
    ecs_doc_id: str | None = None,
    actor_ip: str | None = None,
    host: str | None = None,
) -> RecordResult:
    """Persist an artefact with full evidence-quality metadata.

    Steps, in order:
      1. Validate the case exists.
      2. Compute SHA-256.
      3. Upload raw bytes to the vault (Object Lock enforced by bucket policy).
      4. Build and sign the manifest; upload the manifest.
      5. INSERT evidence_items row.
      6. INSERT the first ``collected`` custody event.
      7. Audit-log success.

    Raises:
        ValidationError, StorageError, SignatureError, CustodyError, ConfigError.
    """
    pii_tags = pii_tags or []
    artefact_id = str(uuid.uuid4())
    sk, vk = signing_config_keys(cfg.signing)

    # ---- 1. Case existence ---------------------------------------------
    try:
        with reader_conn(cfg.postgres) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM cases WHERE case_id = %s", (case_id,))
            if cur.fetchone() is None:
                raise ValidationError("case_id not found", case_id=case_id)
    except psycopg.Error as exc:
        raise CustodyError("case lookup failed", case_id=case_id) from exc

    # ---- 2. Hash --------------------------------------------------------
    digest = sha256_hex(data)

    # ---- 3. Upload artefact --------------------------------------------
    art_key = artefact_key(case_id, artefact_id)
    retention_days = _retention_days_from_class(cfg.retention_class)
    try:
        stored_art = put_artefact(
            cfg.s3,
            key=art_key,
            data=data,
            content_type=mime_type,
            retention_days=retention_days,
        )
    except StorageError:
        _audit(
            cfg,
            actor=collector_principal,
            action="record_evidence",
            target=artefact_id,
            outcome="fail",
            stage="put_artefact",
        )
        raise

    # ---- 4. Build & sign manifest --------------------------------------
    manifest = Manifest(
        artefact_id=artefact_id,
        case_id=case_id,
        source_system=source_system,
        collection_method=collection_method,
        query=query,
        collector_principal=collector_principal,
        collected_at_utc=utc_now_iso(),
        original_tz=original_tz,
        bytes=len(data),
        sha256=digest,
        mime_type=mime_type,
        s3_uri=stored_art.s3_uri,
        pii_tags=list(pii_tags),
        retention_class=cfg.retention_class,
        signing_key_id=cfg.signing.key_id,
        ecs_index=ecs_index,
        ecs_doc_id=ecs_doc_id,
    )
    manifest.signature = sign(sk, manifest.canonical_bytes_for_signing())
    manifest.validate()

    man_key = manifest_key(case_id, artefact_id)
    try:
        stored_manifest = put_manifest(
            cfg.s3,
            key=man_key,
            manifest_bytes=manifest.to_json_bytes(),
            retention_days=retention_days,
        )
    except StorageError:
        _audit(
            cfg,
            actor=collector_principal,
            action="record_evidence",
            target=artefact_id,
            outcome="fail",
            stage="put_manifest",
        )
        raise

    # ---- 5. evidence_items row -----------------------------------------
    try:
        with writer_conn(cfg.postgres) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO evidence_items (
                    artefact_id, case_id, source_system, collection_method, query,
                    collector_principal, collected_at_utc, original_tz, bytes,
                    sha256, mime_type, s3_uri, ecs_index, ecs_doc_id, pii_tags,
                    retention_class, manifest_uri, signing_key_id, manifest_version
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s)
                """,
                (
                    artefact_id,
                    case_id,
                    source_system,
                    collection_method,
                    query,
                    collector_principal,
                    manifest.collected_at_utc,
                    original_tz,
                    len(data),
                    bytes.fromhex(digest),
                    mime_type,
                    stored_art.s3_uri,
                    ecs_index,
                    ecs_doc_id,
                    list(pii_tags),
                    cfg.retention_class,
                    stored_manifest.s3_uri,
                    cfg.signing.key_id,
                    manifest.manifest_version,
                ),
            )
            conn.commit()
    except psycopg.Error as exc:
        _audit(
            cfg,
            actor=collector_principal,
            action="record_evidence",
            target=artefact_id,
            outcome="fail",
            stage="insert_evidence_items",
            pgcode=getattr(getattr(exc, "diag", None), "sqlstate", None),
        )
        raise CustodyError("insert evidence_items failed", artefact_id=artefact_id) from exc

    # ---- 6. First custody event ----------------------------------------
    custody = append_event(
        cfg.postgres,
        artefact_id=artefact_id,
        event_type="collected",
        actor=collector_principal,
        purpose=f"initial collection via {collection_method} from {source_system}",
        signing_key=sk,
        signing_key_id=cfg.signing.key_id,
        actor_ip=actor_ip,
        host=host,
        event_time_utc=manifest.collected_at_utc,
    )

    # ---- 7. Audit-log success ------------------------------------------
    _audit(
        cfg,
        actor=collector_principal,
        action="record_evidence",
        target=artefact_id,
        outcome="ok",
        case_id=case_id,
        source_system=source_system,
        bytes=len(data),
        sha256=digest,
    )

    log.info(
        "evidence recorded",
        extra={
            "context": {
                "artefact_id": artefact_id,
                "case_id": case_id,
                "source_system": source_system,
                "bytes": len(data),
                "sha256": digest,
            }
        },
    )

    return RecordResult(
        artefact_id=artefact_id,
        manifest=manifest,
        s3_uri=stored_art.s3_uri,
        manifest_uri=stored_manifest.s3_uri,
        custody_event=custody,
    )


# ---------------------------------------------------------------------------
# verify_evidence
# ---------------------------------------------------------------------------

def verify_evidence(
    cfg: EvidenceConfig,
    *,
    artefact_id: str,
    accessor: str,
    purpose: str = "verification",
    record_access_event: bool = True,
) -> VerificationReport:
    """Independently re-prove every integrity claim for an artefact.

    Always returns a report. ``report.ok`` is False if any step fails; the
    individual ``steps`` list shows where the chain broke.
    """
    report = VerificationReport(artefact_id=artefact_id, ok=True)
    _, vk = signing_config_keys(cfg.signing)

    # Load the evidence_items row.
    try:
        with reader_conn(cfg.postgres) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT case_id, sha256, s3_uri, manifest_uri, bytes
                  FROM evidence_items
                 WHERE artefact_id = %s
                """,
                (artefact_id,),
            )
            row = cur.fetchone()
    except psycopg.Error as exc:
        report.ok = False
        report.error = "db lookup failed"
        report.add("lookup_evidence_items", ok=False, err=str(exc))
        return report

    if row is None:
        report.ok = False
        report.error = "unknown artefact"
        report.add("lookup_evidence_items", ok=False, reason="not found")
        return report

    case_id, db_sha256, s3_uri, manifest_uri, expected_bytes = row
    expected_sha256 = bytes(db_sha256).hex()
    report.add(
        "lookup_evidence_items",
        ok=True,
        case_id=str(case_id),
        s3_uri=s3_uri,
        expected_sha256=expected_sha256,
        expected_bytes=expected_bytes,
    )

    # Re-download the artefact and re-hash.
    art_key = artefact_key(str(case_id), artefact_id)
    try:
        raw = get_artefact(cfg.s3, key=art_key)
    except StorageError as exc:
        report.ok = False
        report.error = "artefact download failed"
        report.add("get_artefact", ok=False, err=str(exc))
        _audit(cfg, actor=accessor, action="verify_evidence", target=artefact_id, outcome="fail", stage="get_artefact")
        return report

    actual_sha256 = sha256_hex(raw)
    if actual_sha256 != expected_sha256 or len(raw) != expected_bytes:
        report.ok = False
        report.error = "artefact bytes do not match recorded hash"
        report.add(
            "rehash_artefact",
            ok=False,
            actual_sha256=actual_sha256,
            actual_bytes=len(raw),
        )
    else:
        report.add("rehash_artefact", ok=True, actual_sha256=actual_sha256)

    # Re-download manifest, verify signature.
    man_key = manifest_key(str(case_id), artefact_id)
    try:
        manifest_bytes = get_manifest(cfg.s3, key=man_key)
        manifest_dict = json.loads(manifest_bytes.decode("utf-8"))
        manifest = Manifest.from_dict(manifest_dict)
        verify(vk, manifest.canonical_bytes_for_signing(), manifest.signature)
        report.add("verify_manifest_signature", ok=True, signing_key_id=manifest.signing_key_id)
    except (StorageError, json.JSONDecodeError, SignatureError, TypeError) as exc:
        report.ok = False
        report.error = "manifest signature invalid"
        report.add("verify_manifest_signature", ok=False, err=str(exc))

    # Cross-check manifest sha256 against the freshly computed one.
    try:
        if "manifest" in locals() and manifest.sha256 != actual_sha256:
            report.ok = False
            report.error = "manifest sha256 differs from recomputed sha256"
            report.add(
                "manifest_vs_rehash",
                ok=False,
                manifest_sha256=manifest.sha256,
                actual_sha256=actual_sha256,
            )
        else:
            report.add("manifest_vs_rehash", ok=True)
    except NameError:
        pass  # Already reported above.

    # Replay the custody chain.
    try:
        events = replay_chain(cfg.postgres, artefact_id=artefact_id, verify_key=vk)
        report.add("replay_custody_chain", ok=True, events=len(events))
    except CustodyError as exc:
        report.ok = False
        report.error = report.error or "custody chain broken"
        report.add("replay_custody_chain", ok=False, err=str(exc))

    # Record the access event (unless the caller is doing read-only diagnostics).
    if record_access_event and report.ok:
        try:
            sk_for_access, _ = signing_config_keys(cfg.signing)
            append_event(
                cfg.postgres,
                artefact_id=artefact_id,
                event_type="accessed",
                actor=accessor,
                purpose=purpose,
                signing_key=sk_for_access,
                signing_key_id=cfg.signing.key_id,
            )
        except CustodyError as exc:
            # Verification itself succeeded; recording the access failed. Log
            # and surface but don't flip the report's overall outcome.
            log.error(
                "could not record verification access event",
                extra={"context": {"artefact_id": artefact_id, "err": str(exc)}},
            )

    _audit(
        cfg,
        actor=accessor,
        action="verify_evidence",
        target=artefact_id,
        outcome="ok" if report.ok else "fail",
        steps=len(report.steps),
    )

    return report


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _retention_days_from_class(retention_class: str) -> int:
    """Map a retention class label to a day count.

    Demo classes only. Production extends this with a config-driven table.
    """
    mapping = {
        "demo_24h": 1,
        "demo_7d": 7,
        "demo_90d": 90,
        "standard_7y": 7 * 365,
        "legal_hold": 7 * 365,
    }
    if retention_class not in mapping:
        raise ValidationError("unknown retention_class", value=retention_class)
    return mapping[retention_class]
