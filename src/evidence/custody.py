"""Chain-of-custody ledger operations.

The DB enforces append-only via triggers (db/0001_evidence_schema.sql).
This module computes and verifies the hash chain in application code.

Hash chain rule, per artefact:
    row.this_event_hash = sha256(
        canonical_json({
            "event_id":       <uuid>,
            "artefact_id":    <uuid>,
            "event_type":     <enum>,
            "actor":          <text>,
            "actor_ip":       <text|null>,
            "host":           <text|null>,
            "purpose":        <text>,
            "event_time_utc": <iso>,
            "prev_event_hash": <hex>,
        })
    )

The first event for an artefact uses ``prev_event_hash = 32 zero bytes``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg
from nacl.signing import SigningKey, VerifyKey

from .config import PostgresConfig
from .db import reader_conn, writer_conn
from .errors import CustodyError
from .signing import sign as sign_bytes
from .signing import verify as verify_bytes

ZERO_HASH = b"\x00" * 32


@dataclass(slots=True, frozen=True)
class CustodyEvent:
    event_id: str
    artefact_id: str
    event_type: str
    actor: str
    actor_ip: str | None
    host: str | None
    purpose: str
    event_time_utc: str
    prev_event_hash: bytes
    this_event_hash: bytes
    signature_b64: str
    signing_key_id: str


def _canonical_event_bytes(
    *,
    event_id: str,
    artefact_id: str,
    event_type: str,
    actor: str,
    actor_ip: str | None,
    host: str | None,
    purpose: str,
    event_time_utc: str,
    prev_event_hash: bytes,
) -> bytes:
    payload: dict[str, Any] = {
        "event_id": event_id,
        "artefact_id": artefact_id,
        "event_type": event_type,
        "actor": actor,
        "actor_ip": actor_ip,
        "host": host,
        "purpose": purpose,
        "event_time_utc": event_time_utc,
        "prev_event_hash": prev_event_hash.hex(),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def latest_chain_tip(cfg: PostgresConfig, artefact_id: str) -> bytes:
    """Return the most recent ``this_event_hash`` for this artefact, or
    ``ZERO_HASH`` if no events exist yet."""
    with reader_conn(cfg) as conn, conn.cursor() as cur:
        try:
            cur.execute(
                """
                SELECT this_event_hash
                  FROM evidence_custody
                 WHERE artefact_id = %s
                 ORDER BY event_time_utc DESC, event_id DESC
                 LIMIT 1
                """,
                (artefact_id,),
            )
            row = cur.fetchone()
        except psycopg.Error as exc:
            raise CustodyError("latest_chain_tip query failed", artefact_id=artefact_id) from exc
    if row is None:
        return ZERO_HASH
    return bytes(row[0])


def append_event(
    cfg: PostgresConfig,
    *,
    artefact_id: str,
    event_type: str,
    actor: str,
    purpose: str,
    signing_key: SigningKey,
    signing_key_id: str,
    actor_ip: str | None = None,
    host: str | None = None,
    event_time_utc: str | None = None,
) -> CustodyEvent:
    """Compute the hash chain and INSERT one custody row.

    ``signing_key`` signs ``this_event_hash``. The DB has a CHECK that
    prev/this hashes are exactly 32 bytes; we compute them here.
    """
    event_id = str(uuid.uuid4())
    when = event_time_utc or datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    prev = latest_chain_tip(cfg, artefact_id)
    canonical = _canonical_event_bytes(
        event_id=event_id,
        artefact_id=artefact_id,
        event_type=event_type,
        actor=actor,
        actor_ip=actor_ip,
        host=host,
        purpose=purpose,
        event_time_utc=when,
        prev_event_hash=prev,
    )
    this_hash = hashlib.sha256(canonical).digest()
    sig_b64 = sign_bytes(signing_key, this_hash)

    with writer_conn(cfg) as conn, conn.cursor() as cur:
        try:
            cur.execute(
                """
                INSERT INTO evidence_custody (
                    event_id, artefact_id, event_type, actor, actor_ip, host,
                    purpose, event_time_utc, prev_event_hash, this_event_hash,
                    signature, signing_key_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    event_id,
                    artefact_id,
                    event_type,
                    actor,
                    actor_ip,
                    host,
                    purpose,
                    when,
                    prev,
                    this_hash,
                    sig_b64.encode("ascii"),
                    signing_key_id,
                ),
            )
            conn.commit()
        except psycopg.Error as exc:
            conn.rollback()
            raise CustodyError(
                "append_event INSERT failed",
                artefact_id=artefact_id,
                event_type=event_type,
                pgcode=getattr(exc.diag, "sqlstate", None) if hasattr(exc, "diag") else None,
            ) from exc

    return CustodyEvent(
        event_id=event_id,
        artefact_id=artefact_id,
        event_type=event_type,
        actor=actor,
        actor_ip=actor_ip,
        host=host,
        purpose=purpose,
        event_time_utc=when,
        prev_event_hash=prev,
        this_event_hash=this_hash,
        signature_b64=sig_b64,
        signing_key_id=signing_key_id,
    )


def replay_chain(
    cfg: PostgresConfig,
    *,
    artefact_id: str,
    verify_key: VerifyKey,
) -> list[CustodyEvent]:
    """Read every custody event for an artefact in order, recompute the chain,
    and verify each signature. Raises ``CustodyError`` on the first mismatch."""
    with reader_conn(cfg) as conn, conn.cursor() as cur:
        try:
            cur.execute(
                """
                SELECT event_id, event_type, actor, actor_ip::text, host,
                       purpose, event_time_utc, prev_event_hash, this_event_hash,
                       signature, signing_key_id
                  FROM evidence_custody
                 WHERE artefact_id = %s
                 ORDER BY event_time_utc ASC, event_id ASC
                """,
                (artefact_id,),
            )
            rows = cur.fetchall()
        except psycopg.Error as exc:
            raise CustodyError("replay_chain query failed", artefact_id=artefact_id) from exc

    out: list[CustodyEvent] = []
    expected_prev = ZERO_HASH
    for row in rows:
        (
            event_id,
            event_type,
            actor,
            actor_ip,
            host,
            purpose,
            event_time_utc,
            prev_event_hash,
            this_event_hash,
            signature,
            signing_key_id,
        ) = row
        prev_bytes = bytes(prev_event_hash)
        this_bytes = bytes(this_event_hash)
        if prev_bytes != expected_prev:
            raise CustodyError(
                "chain break: prev_event_hash does not match previous row",
                artefact_id=artefact_id,
                event_id=str(event_id),
                expected=expected_prev.hex(),
                got=prev_bytes.hex(),
            )
        when_iso = (
            event_time_utc.isoformat(timespec="milliseconds")
            if isinstance(event_time_utc, datetime)
            else str(event_time_utc)
        )
        canonical = _canonical_event_bytes(
            event_id=str(event_id),
            artefact_id=artefact_id,
            event_type=str(event_type),
            actor=actor,
            actor_ip=actor_ip,
            host=host,
            purpose=purpose,
            event_time_utc=when_iso,
            prev_event_hash=prev_bytes,
        )
        recomputed = hashlib.sha256(canonical).digest()
        if recomputed != this_bytes:
            raise CustodyError(
                "chain break: this_event_hash does not match recomputed digest",
                artefact_id=artefact_id,
                event_id=str(event_id),
            )
        sig_b64 = (signature if isinstance(signature, str) else bytes(signature).decode("ascii"))
        try:
            verify_bytes(verify_key, this_bytes, sig_b64)
        except Exception as exc:  # SignatureError specifically; widen for safety
            raise CustodyError(
                "custody row signature invalid",
                artefact_id=artefact_id,
                event_id=str(event_id),
            ) from exc

        out.append(
            CustodyEvent(
                event_id=str(event_id),
                artefact_id=artefact_id,
                event_type=str(event_type),
                actor=actor,
                actor_ip=actor_ip,
                host=host,
                purpose=purpose,
                event_time_utc=when_iso,
                prev_event_hash=prev_bytes,
                this_event_hash=this_bytes,
                signature_b64=sig_b64,
                signing_key_id=signing_key_id,
            )
        )
        expected_prev = this_bytes
    return out
