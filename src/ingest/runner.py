"""Ingest stage runner — ADR 0003.

Orchestrates: watermark resolution → connector fetch → OpenSearch bulk write
→ pipeline_runs watermark update.

Raw Chat messages are written to OpenSearch only. Pipeline state (watermarks,
errors) is tracked in PostgreSQL. No evidence artefacts are written to MinIO.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import traceback
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import psycopg
import yaml

from evidence.config import PostgresConfig

from .connectors.google_chat import GoogleChatConnector, config_from_yaml
from .errors import ConnectorError, ConnectorTransientError, ConfigError
from .protocol import RawEvent

log = logging.getLogger(__name__)

_DEFAULT_LOOKBACK_HOURS = 24


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class OpenSearchConfig:
    host: str
    port: int


@dataclass(frozen=True, slots=True)
class IngestConfig:
    batch_write_size: int
    index_prefix: str
    opensearch: OpenSearchConfig


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    ingest: IngestConfig
    connector: GoogleChatConnector
    postgres: PostgresConfig


def load_config(config_path: Path) -> PipelineConfig:
    if not config_path.exists():
        raise ConfigError(f"pipeline config not found: {config_path}")

    with config_path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ConfigError(f"pipeline config is not a YAML mapping: {config_path}")

    ingest_raw = raw.get("ingest", {})
    os_raw = raw.get("opensearch", {})

    ingest_cfg = IngestConfig(
        batch_write_size=int(ingest_raw.get("batch_write_size", 500)),
        index_prefix=str(ingest_raw.get("index_prefix", "raw-events")),
        opensearch=OpenSearchConfig(
            host=os.environ.get("OPENSEARCH_HOST") or os_raw.get("host", "127.0.0.1"),
            port=int(os.environ.get("OPENSEARCH_PORT") or os_raw.get("port", 9200)),
        ),
    )

    chat_raw = ingest_raw.get("connectors", {}).get("google_chat", {})
    connector = GoogleChatConnector(config_from_yaml(chat_raw))

    postgres = _load_postgres_config()

    return PipelineConfig(ingest=ingest_cfg, connector=connector, postgres=postgres)


def _load_postgres_config() -> PostgresConfig:
    def _req(name: str) -> str:
        v = os.environ.get(name)
        if not v:
            raise ConfigError(f"required environment variable missing: {name}")
        return v

    def _opt(name: str, default: str) -> str:
        return os.environ.get(name) or default

    return PostgresConfig(
        host=_opt("POSTGRES_HOST", "127.0.0.1"),
        port=int(_opt("POSTGRES_PORT", "5432")),
        database=_req("POSTGRES_DB"),
        writer_user=_req("POSTGRES_WRITER_USER"),
        writer_password=_req("POSTGRES_WRITER_PASSWORD"),
        reader_user=_req("POSTGRES_READER_USER"),
        reader_password=_req("POSTGRES_READER_PASSWORD"),
    )


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class IngestResult:
    run_id: uuid.UUID
    pipeline_run_id: uuid.UUID
    watermark_start: datetime
    watermark_end: datetime
    records_in: int
    records_out: int
    dead_lettered: int
    status: str  # 'completed' | 'partial' | 'failed'


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(
    config_path: Path,
    *,
    pipeline_run_id: uuid.UUID | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    dry_run: bool = False,
) -> IngestResult:
    cfg = load_config(config_path)
    pipeline_run_id = pipeline_run_id or uuid.uuid4()
    run_id = uuid.uuid4()
    config_hash = _hash_config(config_path)

    watermark_start, watermark_end = _resolve_watermark(
        cfg.postgres, start_override=start, end_override=end
    )

    log.info(
        "ingest.runner.start",
        extra={"context": {
            "run_id": str(run_id),
            "pipeline_run_id": str(pipeline_run_id),
            "watermark_start": watermark_start.isoformat(),
            "watermark_end": watermark_end.isoformat(),
            "dry_run": dry_run,
        }},
    )

    if not dry_run:
        _upsert_pipeline_run(
            cfg.postgres,
            run_id=run_id,
            pipeline_run_id=pipeline_run_id,
            stage="ingest",
            status="running",
            watermark_start=watermark_start,
            watermark_end=watermark_end,
            config_hash=config_hash,
        )

    health = cfg.connector.health_check()
    if not health.healthy:
        log.error(
            "ingest.runner.unhealthy",
            extra={"context": {"detail": health.detail}},
        )
        if not dry_run:
            _update_pipeline_run(
                cfg.postgres, run_id=run_id, status="failed",
                records_in=0, records_out=0,
                error_message=f"health check failed: {health.detail}",
            )
        return IngestResult(
            run_id=run_id, pipeline_run_id=pipeline_run_id,
            watermark_start=watermark_start, watermark_end=watermark_end,
            records_in=0, records_out=0, dead_lettered=0, status="failed",
        )

    records_in = 0
    records_out = 0
    dead_lettered = 0
    batch: list[RawEvent] = []

    def _flush(b: list[RawEvent]) -> None:
        nonlocal records_out
        if not b:
            return
        written, _ = _bulk_write_opensearch(
            b,
            os_host=cfg.ingest.opensearch.host,
            os_port=cfg.ingest.opensearch.port,
            index_prefix=cfg.ingest.index_prefix,
            dry_run=dry_run,
        )
        records_out += written

    try:
        for event in cfg.connector.fetch(
            start=watermark_start, end=watermark_end, run_id=run_id
        ):
            records_in += 1
            batch.append(event)
            if len(batch) >= cfg.ingest.batch_write_size:
                _flush(batch)
                batch = []

        _flush(batch)

    except ConnectorError as exc:
        log.error(
            "ingest.runner.connector_error",
            extra={"context": {"err": str(exc), **exc.context}},
        )
        dead_lettered += 1
        if not dry_run:
            _dead_letter(
                cfg.postgres, run_id=run_id, stage="ingest",
                attempt=1, exc=exc, payload_sha256=None,
            )
            _update_pipeline_run(
                cfg.postgres, run_id=run_id, status="failed",
                records_in=records_in, records_out=records_out,
                error_message=str(exc),
            )
        return IngestResult(
            run_id=run_id, pipeline_run_id=pipeline_run_id,
            watermark_start=watermark_start, watermark_end=watermark_end,
            records_in=records_in, records_out=records_out,
            dead_lettered=dead_lettered, status="failed",
        )

    final_status = "completed" if dead_lettered == 0 else "partial"
    if not dry_run:
        _update_pipeline_run(
            cfg.postgres, run_id=run_id, status=final_status,
            records_in=records_in, records_out=records_out,
        )

    log.info(
        "ingest.runner.complete",
        extra={"context": {
            "run_id": str(run_id),
            "records_in": records_in,
            "records_out": records_out,
            "dead_lettered": dead_lettered,
            "status": final_status,
        }},
    )

    return IngestResult(
        run_id=run_id, pipeline_run_id=pipeline_run_id,
        watermark_start=watermark_start, watermark_end=watermark_end,
        records_in=records_in, records_out=records_out,
        dead_lettered=dead_lettered, status=final_status,
    )


# ---------------------------------------------------------------------------
# Watermark
# ---------------------------------------------------------------------------

def _resolve_watermark(
    pg_cfg: PostgresConfig,
    *,
    start_override: datetime | None,
    end_override: datetime | None,
) -> tuple[datetime, datetime]:
    end = end_override or datetime.now(timezone.utc)
    if start_override:
        return start_override, end

    try:
        with _writer_conn(pg_cfg) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT watermark_end
                  FROM pipeline_runs
                 WHERE stage = 'ingest'
                   AND status IN ('completed', 'partial')
                   AND config_hash LIKE %s
                 ORDER BY completed_at_utc DESC
                 LIMIT 1
                """,
                ("%google_workspace.chat%",),
            )
            row = cur.fetchone()
    except psycopg.Error as exc:
        log.warning(
            "ingest.runner.watermark_lookup_failed",
            extra={"context": {"err": str(exc)}},
        )
        row = None

    if row:
        log.info(
            "ingest.runner.watermark_loaded",
            extra={"context": {"last_end": row[0].isoformat()}},
        )
        return row[0], end

    log.info(
        "ingest.runner.first_run",
        extra={"context": {"default_lookback_hours": _DEFAULT_LOOKBACK_HOURS}},
    )
    return end - timedelta(hours=_DEFAULT_LOOKBACK_HOURS), end


# ---------------------------------------------------------------------------
# OpenSearch bulk write
# ---------------------------------------------------------------------------

def _bulk_write_opensearch(
    batch: list[RawEvent],
    *,
    os_host: str,
    os_port: int,
    index_prefix: str,
    dry_run: bool,
) -> tuple[int, int]:  # (written, skipped_dupes)
    if dry_run:
        for event in batch:
            log.debug(
                "ingest.opensearch.dry_run",
                extra={"context": {
                    "event_id": event.event_id,
                    "source": event.source_system,
                }},
            )
        return len(batch), 0

    lines: list[str] = []
    for event in batch:
        doc_id = hashlib.sha256(
            f"{event.source_system}:{event.event_id}".encode()
        ).hexdigest()
        index = (
            f"{index_prefix}-{event.source_system.replace('.', '_')}"
            f"-{event.occurred_at_utc:%Y.%m.%d}"
        )
        action = json.dumps({"create": {"_index": index, "_id": doc_id}})
        doc = json.dumps(
            {
                "source_system": event.source_system,
                "event_id": event.event_id,
                "occurred_at_utc": event.occurred_at_utc.isoformat(),
                "original_timezone": event.original_timezone,
                "collected_at_utc": event.collected_at_utc.isoformat(),
                "sha256": event.sha256,
                **event.raw_json,
            },
            default=str,
        )
        lines.extend([action, doc])

    body = "\n".join(lines) + "\n"
    url = f"http://{os_host}:{os_port}/_bulk"

    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/x-ndjson"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise ConnectorTransientError(
            f"OpenSearch bulk write failed: {exc}", url=url
        ) from exc

    written = 0
    dupes = 0
    for item in result.get("items", []):
        status = item.get("create", {}).get("status", 0)
        if status in (200, 201):
            written += 1
        elif status == 409:
            dupes += 1
        else:
            log.warning(
                "ingest.opensearch.item_error",
                extra={"context": {"item": item}},
            )

    log.info(
        "ingest.opensearch.bulk_write",
        extra={"context": {
            "written": written,
            "dupes": dupes,
            "batch_size": len(batch),
        }},
    )
    return written, dupes


# ---------------------------------------------------------------------------
# Pipeline run management
# ---------------------------------------------------------------------------

def _upsert_pipeline_run(
    pg_cfg: PostgresConfig,
    *,
    run_id: uuid.UUID,
    pipeline_run_id: uuid.UUID,
    stage: str,
    status: str,
    watermark_start: datetime,
    watermark_end: datetime,
    config_hash: str,
) -> None:
    try:
        with _writer_conn(pg_cfg) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pipeline_runs (
                    run_id, pipeline_run_id, stage, status,
                    watermark_start, watermark_end, config_hash
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (pipeline_run_id, stage)
                DO UPDATE SET status = EXCLUDED.status
                """,
                (
                    str(run_id), str(pipeline_run_id), stage, status,
                    watermark_start, watermark_end, config_hash,
                ),
            )
            conn.commit()
    except psycopg.Error as exc:
        log.error(
            "ingest.runner.pipeline_run_insert_failed",
            extra={"context": {"err": str(exc)}},
        )


def _update_pipeline_run(
    pg_cfg: PostgresConfig,
    *,
    run_id: uuid.UUID,
    status: str,
    records_in: int,
    records_out: int,
    error_message: str | None = None,
) -> None:
    try:
        with _writer_conn(pg_cfg) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE pipeline_runs
                   SET status = %s,
                       records_in = %s,
                       records_out = %s,
                       error_message = %s,
                       completed_at_utc = (now() AT TIME ZONE 'UTC')
                 WHERE run_id = %s
                """,
                (status, records_in, records_out, error_message, str(run_id)),
            )
            conn.commit()
    except psycopg.Error as exc:
        log.error(
            "ingest.runner.pipeline_run_update_failed",
            extra={"context": {"err": str(exc)}},
        )


def _dead_letter(
    pg_cfg: PostgresConfig,
    *,
    run_id: uuid.UUID,
    stage: str,
    attempt: int,
    exc: Exception,
    payload_sha256: str | None,
) -> None:
    try:
        with _writer_conn(pg_cfg) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pipeline_errors (
                    run_id, stage, attempt, error_class, error_message,
                    traceback, payload_sha256
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(run_id), stage, attempt,
                    type(exc).__name__, str(exc),
                    traceback.format_exc(), payload_sha256,
                ),
            )
            conn.commit()
    except psycopg.Error as db_exc:
        log.error(
            "ingest.runner.dead_letter_failed",
            extra={"context": {"err": str(db_exc), "original_err": str(exc)}},
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@contextmanager
def _writer_conn(pg_cfg: PostgresConfig) -> Iterator[psycopg.Connection]:
    dsn = (
        f"host={pg_cfg.host} port={pg_cfg.port} dbname={pg_cfg.database} "
        f"user={pg_cfg.writer_user} password={pg_cfg.writer_password} "
        f"application_name=ingest-runner "
        f"options=-c\\ TimeZone=UTC"
    )
    conn = psycopg.connect(dsn)
    try:
        yield conn
    finally:
        conn.close()


def _hash_config(config_path: Path) -> str:
    data = config_path.read_bytes()
    return f"google_workspace.chat:{hashlib.sha256(data).hexdigest()}"
