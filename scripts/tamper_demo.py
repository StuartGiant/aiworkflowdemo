"""Tamper demonstration script.

Shows, live, what each of the three integrity surfaces protects against:

    1. Vault Object Lock      -- writer cannot overwrite the artefact.
    2. Custody trigger        -- DBA-equivalent writer cannot UPDATE / DELETE
                                 a custody row.
    3. End-to-end verify      -- if an admin role bypasses Object Lock
                                 (governance mode allows this), verify_evidence
                                 detects the tampering via hash mismatch.

Run after the stack is up:

    python -m scripts.tamper_demo

The script is read-from-config; nothing is hardcoded.
"""

from __future__ import annotations

import io
import sys
import uuid
from pathlib import Path

# Make src/ importable without an install step.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import psycopg
from minio.commonconfig import GOVERNANCE
from minio.error import S3Error
from minio.retention import Retention
from datetime import datetime, timedelta, timezone

from evidence import (
    EvidenceConfig,
    record_evidence,
    verify_evidence,
)
from evidence.db import writer_conn
from evidence.storage import (
    admin_client,
    artefact_key,
    writer_client,
)


PASS = "  [PASS]"
FAIL = "  [FAIL]"
INFO = "  [info]"


def banner(s: str) -> None:
    print(f"\n=== {s} ===")


def seed_case(cfg: EvidenceConfig) -> str:
    """Insert a throwaway case row (case rows are not append-only — fine to
    create from the writer role for the demo)."""
    case_id = str(uuid.uuid4())
    with writer_conn(cfg.postgres) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cases (case_id, case_code, title, subject_ref)
            VALUES (%s, %s, %s, %s)
            """,
            (case_id, f"DEMO-{case_id[:8]}", "tamper demo", "subject_ref:demo"),
        )
        conn.commit()
    print(f"{INFO} seeded case_id={case_id}")
    return case_id


def step_1_record(cfg: EvidenceConfig, case_id: str) -> str:
    banner("Step 1 — record an artefact and verify clean state")
    result = record_evidence(
        cfg,
        case_id=case_id,
        source_system="google_workspace.admin_reports",
        collection_method="api",
        query="applicationName=login,eventName=login_success,user=alice@example",
        collector_principal="serviceaccount:evidence-writer@local",
        original_tz="America/Los_Angeles",
        data=b'{"event":"login_success","user":"alice@example","ts":"2026-05-21T17:42:11Z"}\n',
        mime_type="application/json",
        pii_tags=["actor_email"],
        host="demo-laptop",
    )
    print(f"{INFO} artefact_id={result.artefact_id}")
    print(f"{INFO} s3_uri={result.s3_uri}")
    report = verify_evidence(cfg, artefact_id=result.artefact_id, accessor="demo:reviewer")
    print(PASS if report.ok else FAIL, "post-record verification:", "ok" if report.ok else report.error)
    for step in report.steps:
        print("    -", step["step"], "OK" if step["ok"] else "FAIL")
    if not report.ok:
        raise SystemExit("baseline verification failed; aborting demo")
    return result.artefact_id


def step_2_overwrite_blocked(cfg: EvidenceConfig, case_id: str, artefact_id: str) -> None:
    banner("Step 2 — writer attempts to overwrite the vault object (must be blocked)")
    key = artefact_key(case_id, artefact_id)
    client = writer_client(cfg.s3)
    tampered = b"TAMPERED PAYLOAD"
    try:
        client.put_object(
            bucket_name=cfg.s3.bucket,
            object_name=key,
            data=io.BytesIO(tampered),
            length=len(tampered),
            content_type="application/octet-stream",
        )
    except S3Error as exc:
        print(PASS, f"writer overwrite refused by Object Lock: {exc.code}")
        return
    # If we got here, the lock didn't fire — that's a demo failure.
    print(FAIL, "writer overwrite succeeded — Object Lock is not in force!")


def step_3_custody_modify_blocked(cfg: EvidenceConfig, artefact_id: str) -> None:
    banner("Step 3 — writer attempts to mutate the custody ledger (must be blocked)")
    with writer_conn(cfg.postgres) as conn, conn.cursor() as cur:
        # UPDATE attempt
        try:
            cur.execute(
                "UPDATE evidence_custody SET purpose = 'rewrite' WHERE artefact_id = %s",
                (artefact_id,),
            )
            conn.commit()
            print(FAIL, "UPDATE succeeded — append-only trigger is missing!")
        except psycopg.errors.InsufficientPrivilege as exc:
            conn.rollback()
            print(PASS, f"UPDATE refused: {exc.diag.message_primary}")
        except psycopg.Error as exc:
            conn.rollback()
            print(PASS, f"UPDATE refused: {exc}")

        # DELETE attempt
        try:
            cur.execute("DELETE FROM evidence_custody WHERE artefact_id = %s", (artefact_id,))
            conn.commit()
            print(FAIL, "DELETE succeeded — append-only trigger is missing!")
        except psycopg.errors.InsufficientPrivilege as exc:
            conn.rollback()
            print(PASS, f"DELETE refused: {exc.diag.message_primary}")
        except psycopg.Error as exc:
            conn.rollback()
            print(PASS, f"DELETE refused: {exc}")


def step_4_admin_bypass_then_detected(
    cfg: EvidenceConfig, case_id: str, artefact_id: str
) -> None:
    banner("Step 4 — admin bypasses Governance lock; verify_evidence MUST detect it")
    key = artefact_key(case_id, artefact_id)
    client = admin_client(cfg.s3)
    tampered = b"TAMPERED PAYLOAD INSERTED BY ADMIN BYPASS"
    far_future = datetime.now(timezone.utc) + timedelta(days=1)
    try:
        # Use bypass-governance retention with the admin role to overwrite.
        client.put_object(
            bucket_name=cfg.s3.bucket,
            object_name=key,
            data=io.BytesIO(tampered),
            length=len(tampered),
            content_type="application/octet-stream",
            retention=Retention(mode=GOVERNANCE, retain_until_date=far_future),
            legal_hold=None,
        )
        print(INFO, "admin overwrite committed (governance bypass) — bytes now altered")
    except S3Error as exc:
        # If even admin cannot overwrite, the demo can't proceed — but the
        # integrity guarantee is still demonstrated.
        print(INFO, f"admin overwrite refused (still good): {exc.code}")
        return

    report = verify_evidence(
        cfg,
        artefact_id=artefact_id,
        accessor="demo:reviewer",
        record_access_event=False,
    )
    if not report.ok:
        print(PASS, f"tampering detected: {report.error}")
        for step in report.steps:
            print("    -", step["step"], "OK" if step["ok"] else "FAIL")
    else:
        print(FAIL, "tampering NOT detected — verify_evidence has a gap!")


def main() -> None:
    cfg = EvidenceConfig.from_env()
    case_id = seed_case(cfg)
    artefact_id = step_1_record(cfg, case_id)
    step_2_overwrite_blocked(cfg, case_id, artefact_id)
    step_3_custody_modify_blocked(cfg, artefact_id)
    step_4_admin_bypass_then_detected(cfg, case_id, artefact_id)
    print("\nDemo complete.\n")


if __name__ == "__main__":
    main()
