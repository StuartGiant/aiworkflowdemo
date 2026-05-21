"""Object-store interface for the evidence vault.

Wraps the MinIO Python client. Production swap-out is GCS or S3; this module's
public API is intentionally narrow (``put_artefact``, ``get_artefact``,
``put_manifest``, ``get_manifest``) so the call sites in record_evidence /
verify_evidence do not depend on the underlying SDK.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import BinaryIO

from minio import Minio
from minio.commonconfig import GOVERNANCE
from minio.error import S3Error
from minio.retention import Retention

from .config import S3Config
from .errors import StorageError


@dataclass(slots=True, frozen=True)
class StoredObject:
    s3_uri: str
    etag: str
    version_id: str | None


def _client(cfg: S3Config, access_key: str, secret_key: str) -> Minio:
    # ``secure=False`` because the demo MinIO is on http://127.0.0.1:9000.
    # Production endpoints use HTTPS and ``secure=True``.
    secure = not cfg.endpoint_url.startswith("http://")
    host = cfg.endpoint_url.split("://", 1)[1]
    return Minio(
        endpoint=host,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
        region=cfg.region,
    )


def writer_client(cfg: S3Config) -> Minio:
    return _client(cfg, cfg.writer_access_key, cfg.writer_secret_key)


def reader_client(cfg: S3Config) -> Minio:
    return _client(cfg, cfg.reader_access_key, cfg.reader_secret_key)


def admin_client(cfg: S3Config) -> Minio:
    """ADMIN client — bypasses Governance retention.

    Used ONLY by the tamper-demo script. Never call this from record_evidence
    or verify_evidence.
    """
    return _client(cfg, cfg.admin_access_key, cfg.admin_secret_key)


def _retention_until(days: int) -> Retention:
    until = datetime.now(timezone.utc) + timedelta(days=days)
    return Retention(mode=GOVERNANCE, retain_until_date=until)


def put_artefact(
    cfg: S3Config,
    *,
    key: str,
    data: bytes,
    content_type: str,
    retention_days: int,
) -> StoredObject:
    client = writer_client(cfg)
    body: BinaryIO = io.BytesIO(data)
    try:
        result = client.put_object(
            bucket_name=cfg.bucket,
            object_name=key,
            data=body,
            length=len(data),
            content_type=content_type,
            retention=_retention_until(retention_days),
            legal_hold=None,
        )
    except S3Error as exc:
        raise StorageError(
            "put_artefact failed",
            bucket=cfg.bucket,
            key=key,
            code=exc.code,
        ) from exc
    return StoredObject(
        s3_uri=f"s3://{cfg.bucket}/{key}",
        etag=result.etag,
        version_id=result.version_id,
    )


def put_manifest(
    cfg: S3Config,
    *,
    key: str,
    manifest_bytes: bytes,
    retention_days: int,
) -> StoredObject:
    """Stored under the same governance retention as the artefact itself."""
    client = writer_client(cfg)
    body: BinaryIO = io.BytesIO(manifest_bytes)
    try:
        result = client.put_object(
            bucket_name=cfg.bucket,
            object_name=key,
            data=body,
            length=len(manifest_bytes),
            content_type="application/json",
            retention=_retention_until(retention_days),
            legal_hold=None,
        )
    except S3Error as exc:
        raise StorageError(
            "put_manifest failed",
            bucket=cfg.bucket,
            key=key,
            code=exc.code,
        ) from exc
    return StoredObject(
        s3_uri=f"s3://{cfg.bucket}/{key}",
        etag=result.etag,
        version_id=result.version_id,
    )


def get_artefact(cfg: S3Config, *, key: str) -> bytes:
    client = reader_client(cfg)
    try:
        response = client.get_object(cfg.bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()
    except S3Error as exc:
        raise StorageError(
            "get_artefact failed",
            bucket=cfg.bucket,
            key=key,
            code=exc.code,
        ) from exc


def get_manifest(cfg: S3Config, *, key: str) -> bytes:
    return get_artefact(cfg, key=key)


def artefact_key(case_id: str, artefact_id: str) -> str:
    """Object key layout: <case>/<artefact_id>/raw"""
    return f"{case_id}/{artefact_id}/raw"


def manifest_key(case_id: str, artefact_id: str) -> str:
    return f"{case_id}/{artefact_id}/manifest.json"
