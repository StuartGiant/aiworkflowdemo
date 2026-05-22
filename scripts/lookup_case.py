#!/usr/bin/env python3
"""Case lookup agent — retrieve case information for a specific user.

Queries the local evidence database for all activity associated with a given
username or email address and prints a structured report to stdout.

**Demo scope (Option C):** queries bookmark_violations and entity_hits only.
The cases/findings/evidence_items tables use a pseudonymous subject_ref whose
identity mapping is out of scope for this demo; those tables are not queried
here.  See --help for the production extension path (Options A+B).

Usage:
    python scripts/lookup_case.py --username jsmith
    python scripts/lookup_case.py --email jsmith@corp.com
    python scripts/lookup_case.py --username jsmith --email jsmith@corp.com
    python scripts/lookup_case.py --username jsmith --json

At least one of --username or --email is required.

Must be run inside the project virtual environment:
    source venv/bin/activate
    python scripts/lookup_case.py --username jsmith
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Project root / path bootstrap
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

try:
    from dotenv import load_dotenv  # type: ignore[import]

    load_dotenv(_PROJECT_ROOT / ".env")
except ImportError:
    pass  # dotenv optional; env vars may be set by the shell

# ---------------------------------------------------------------------------
# Logging — structured JSON to stderr so stdout stays clean for report output
# ---------------------------------------------------------------------------

_LOG_HANDLER = logging.StreamHandler(sys.stderr)
_LOG_HANDLER.setFormatter(
    logging.Formatter(
        fmt='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
)
logging.basicConfig(handlers=[_LOG_HANDLER], level=logging.INFO)
logger = logging.getLogger("lookup_case")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _fmt_ts(ts: datetime | None) -> str:
    if ts is None:
        return "—"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _redact_email(email: str | None) -> str:
    """Partially redact an email address for log output (PII minimisation)."""
    if not email:
        return ""
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        return f"{'*' * len(local)}@{domain}"
    return f"{local[0]}{'*' * (len(local) - 2)}{local[-1]}@{domain}"


# ---------------------------------------------------------------------------
# Query functions (all use evidence_reader — read-only)
# ---------------------------------------------------------------------------


def _query_bookmark_violations(
    conn: Any,
    username: str | None,
    email: str | None,
) -> list[dict[str, Any]]:
    """Return bookmark_violations rows matching username OR email."""
    conditions: list[str] = []
    params: list[Any] = []

    if username:
        conditions.append("os_username = %s")
        params.append(username)
    if email:
        conditions.append("chrome_email ILIKE %s")
        params.append(email)

    if not conditions:
        return []

    where = " OR ".join(conditions)
    sql = f"""
        SELECT
            violation_id::text,
            detected_at_utc,
            hostname,
            os_username,
            chrome_profile,
            chrome_email,
            url,
            title,
            item_type,
            pattern_name,
            action_taken,
            action_error,
            notified_at_utc
        FROM bookmark_violations
        WHERE {where}
        ORDER BY detected_at_utc ASC
    """

    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _query_entity_hits(
    conn: Any,
    username: str | None,
) -> list[dict[str, Any]]:
    """Return entity_hits rows where entity_id matches username."""
    if not username:
        return []

    sql = """
        SELECT
            eh.hit_id::text,
            eh.run_id::text,
            eh.event_id,
            eh.source_system,
            eh.entity_type,
            eh.entity_id,
            eh.is_privileged,
            eh.is_asset_critical,
            eh.matched_at_utc,
            pr.stage         AS pipeline_stage,
            pr.status        AS pipeline_status,
            pr.watermark_start,
            pr.watermark_end
        FROM entity_hits eh
        JOIN pipeline_runs pr ON pr.run_id = eh.run_id
        WHERE eh.entity_id = %s
          AND eh.entity_type = 'user'
        ORDER BY eh.matched_at_utc ASC
    """

    with conn.cursor() as cur:
        cur.execute(sql, [username])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Audit log write (uses evidence_writer — INSERT only)
# ---------------------------------------------------------------------------


def _write_audit_log(
    writer_conn: Any,
    actor: str,
    username: str | None,
    email: str | None,
    outcome: str,
    hit_counts: dict[str, int],
) -> None:
    """Append one audit_log row recording this lookup invocation."""
    details: dict[str, Any] = {
        "agent": "lookup_case",
        "query": {
            "username_provided": username is not None,
            "email_provided": email is not None,
            # Redact actual values from the audit log (PII minimisation).
            "email_redacted": _redact_email(email) if email else None,
        },
        "hit_counts": hit_counts,
        "scope": ["bookmark_violations", "entity_hits"],
        "subject_ref_scope": "excluded_demo",
    }

    sql = """
        INSERT INTO audit_log (event_time_utc, actor, action, target, outcome, details)
        VALUES (%s, %s, %s, %s, %s, %s)
    """
    params = [
        _utc_now(),
        actor,
        "case_lookup",
        username or email,
        outcome,
        json.dumps(details),
    ]

    with writer_conn.cursor() as cur:
        cur.execute(sql, params)
    writer_conn.commit()
    logger.info("audit_log entry written — actor=%s outcome=%s", actor, outcome)


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _render_text(
    username: str | None,
    email: str | None,
    violations: list[dict[str, Any]],
    entity_hits: list[dict[str, Any]],
) -> None:
    sep = "=" * 72
    thin = "-" * 72

    print(sep)
    print("  CASE LOOKUP REPORT")
    print(f"  Generated : {_fmt_ts(_utc_now())}")
    print(f"  Subject   : username={username or '—'}  email={email or '—'}")
    print("  Scope     : bookmark_violations, entity_hits  [demo — subject_ref excluded]")
    print(sep)

    # ── Bookmark violations ──────────────────────────────────────────────────
    print(f"\n{'BOOKMARK VIOLATIONS':}")
    print(thin)
    if not violations:
        print("  No records found.")
    else:
        print(f"  {len(violations)} violation(s) found.\n")
        for i, v in enumerate(violations, 1):
            print(f"  [{i}]  Detected  : {_fmt_ts(v['detected_at_utc'])}")
            print(f"       Host      : {v['hostname']}")
            print(f"       OS user   : {v['os_username']}")
            print(f"       Profile   : {v['chrome_profile']}  ({v['chrome_email'] or '—'})")
            print(f"       URL       : {v['url']}")
            print(f"       Title     : {v['title'] or '—'}")
            print(f"       Type      : {v['item_type']}  |  Pattern : {v['pattern_name']}")
            print(f"       Action    : {v['action_taken']}")
            if v["action_error"]:
                print(f"       Error     : {v['action_error']}")
            print(f"       Notified  : {_fmt_ts(v['notified_at_utc'])}")
            print()

    # ── Entity hits ──────────────────────────────────────────────────────────
    print(f"\n{'ENTITY HITS (PIPELINE CORRELATION)':}")
    print(thin)
    if not entity_hits:
        print("  No records found.")
    else:
        print(f"  {len(entity_hits)} hit(s) found.\n")
        for i, h in enumerate(entity_hits, 1):
            flags: list[str] = []
            if h["is_privileged"]:
                flags.append("PRIVILEGED")
            if h["is_asset_critical"]:
                flags.append("CRITICAL_ASSET")
            flag_str = f"  [{', '.join(flags)}]" if flags else ""
            print(f"  [{i}]  Matched   : {_fmt_ts(h['matched_at_utc'])}{flag_str}")
            print(f"       Source    : {h['source_system']}")
            print(f"       Event ID  : {h['event_id']}")
            print(f"       Entity    : {h['entity_type']} / {h['entity_id']}")
            print(f"       Pipeline  : stage={h['pipeline_stage']}  status={h['pipeline_status']}")
            print(f"       Window    : {_fmt_ts(h['watermark_start'])} → {_fmt_ts(h['watermark_end'])}")
            print()

    # ── Full timeline ────────────────────────────────────────────────────────
    print(f"\n{'FULL TIMELINE (chronological)':}")
    print(thin)

    timeline: list[tuple[datetime, str, str]] = []

    for v in violations:
        ts = v["detected_at_utc"]
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        label = (
            f"BOOKMARK_VIOLATION  pattern={v['pattern_name']}  "
            f"action={v['action_taken']}  host={v['hostname']}"
        )
        timeline.append((ts, "bookmark_violations", label))

    for h in entity_hits:
        ts = h["matched_at_utc"]
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        label = (
            f"ENTITY_HIT  source={h['source_system']}  "
            f"event_id={h['event_id']}  stage={h['pipeline_stage']}"
        )
        timeline.append((ts, "entity_hits", label))

    if not timeline:
        print("  No events to display.")
    else:
        for ts, source, label in sorted(timeline, key=lambda x: x[0]):
            print(f"  {_fmt_ts(ts)}  [{source}]  {label}")

    print()
    print(sep)
    print("  NOTE: cases/findings/evidence_items excluded (demo scope).")
    print("        Production build: use --subject-ref or entities YAML to")
    print("        resolve subject_ref and query full case history.")
    print(sep)


def _render_json(
    username: str | None,
    email: str | None,
    violations: list[dict[str, Any]],
    entity_hits: list[dict[str, Any]],
) -> None:
    def _serialise(obj: Any) -> Any:
        if isinstance(obj, datetime):
            return _fmt_ts(obj)
        raise TypeError(f"not serialisable: {type(obj)}")

    report = {
        "generated_at_utc": _fmt_ts(_utc_now()),
        "query": {"username": username, "email": email},
        "scope": ["bookmark_violations", "entity_hits"],
        "subject_ref_scope": "excluded_demo",
        "bookmark_violations": violations,
        "entity_hits": entity_hits,
        "timeline": sorted(
            [
                {
                    "ts": _fmt_ts(
                        v["detected_at_utc"].replace(tzinfo=timezone.utc)
                        if v["detected_at_utc"].tzinfo is None
                        else v["detected_at_utc"]
                    ),
                    "source": "bookmark_violations",
                    "pattern": v["pattern_name"],
                    "action": v["action_taken"],
                    "hostname": v["hostname"],
                }
                for v in violations
                if v["detected_at_utc"]
            ]
            + [
                {
                    "ts": _fmt_ts(
                        h["matched_at_utc"].replace(tzinfo=timezone.utc)
                        if h["matched_at_utc"].tzinfo is None
                        else h["matched_at_utc"]
                    ),
                    "source": "entity_hits",
                    "source_system": h["source_system"],
                    "event_id": h["event_id"],
                    "pipeline_stage": h["pipeline_stage"],
                }
                for h in entity_hits
                if h["matched_at_utc"]
            ],
            key=lambda x: x["ts"],
        ),
    }
    print(json.dumps(report, default=_serialise, indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:  # noqa: C901 (acceptable complexity for a CLI entrypoint)
    parser = argparse.ArgumentParser(
        description=(
            "Retrieve all case-relevant records for a user from the evidence database.\n\n"
            "Demo scope: bookmark_violations + entity_hits only.\n"
            "Production: add --subject-ref or entities YAML for full case history."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--username",
        metavar="USERNAME",
        help="OS username (e.g. jsmith). Matched against bookmark_violations.os_username "
        "and entity_hits.entity_id.",
    )
    parser.add_argument(
        "--email",
        metavar="EMAIL",
        help="Email address. Matched against bookmark_violations.chrome_email (case-insensitive).",
    )
    parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Emit machine-readable JSON to stdout instead of the human report.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug-level logging to stderr.",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.username and not args.email:
        parser.error("at least one of --username or --email is required")

    # ── Import evidence module (after sys.path is set) ───────────────────────
    try:
        from evidence.config import EvidenceConfig
        from evidence.db import reader_conn, writer_conn
    except ImportError as exc:
        logger.error("failed to import evidence module — is venv active? %s", exc)
        return 1

    # ── Load config ──────────────────────────────────────────────────────────
    try:
        cfg = EvidenceConfig.from_env()
    except Exception as exc:  # evidence.errors.ConfigError
        logger.error("configuration error: %s", exc)
        return 1

    actor = os.environ.get("USER", "lookup_agent")
    outcome = "ok"
    hit_counts: dict[str, int] = {"bookmark_violations": 0, "entity_hits": 0}

    violations: list[dict[str, Any]] = []
    hits: list[dict[str, Any]] = []

    # ── Read queries (evidence_reader) ────────────────────────────────────────
    try:
        with reader_conn(cfg.postgres) as rconn:
            logger.debug("querying bookmark_violations")
            violations = _query_bookmark_violations(rconn, args.username, args.email)
            hit_counts["bookmark_violations"] = len(violations)
            logger.info(
                "bookmark_violations: %d row(s) for username=%s email=%s",
                len(violations),
                args.username,
                args.email,
            )

            logger.debug("querying entity_hits")
            hits = _query_entity_hits(rconn, args.username)
            hit_counts["entity_hits"] = len(hits)
            logger.info("entity_hits: %d row(s) for entity_id=%s", len(hits), args.username)

    except Exception as exc:
        logger.error("database read error: %s", exc, exc_info=args.verbose)
        outcome = "fail"
        # Fall through to write the audit log even on failure.

    # ── Audit log (evidence_writer) ───────────────────────────────────────────
    try:
        with writer_conn(cfg.postgres) as wconn:
            _write_audit_log(
                wconn,
                actor=actor,
                username=args.username,
                email=args.email,
                outcome=outcome,
                hit_counts=hit_counts,
            )
    except Exception as exc:
        logger.error("audit log write failed: %s", exc, exc_info=args.verbose)
        # Non-fatal — the lookup still ran; do not suppress the report.

    if outcome == "fail":
        return 1

    # ── Render output ─────────────────────────────────────────────────────────
    if args.output_json:
        _render_json(args.username, args.email, violations, hits)
    else:
        _render_text(args.username, args.email, violations, hits)

    return 0


if __name__ == "__main__":
    sys.exit(main())
