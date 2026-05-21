"""Configuration loader.

Reads from environment variables (populated locally from a gitignored .env).
The same loader is used in cloud deployments where env vars are populated from
GCP Secret Manager via the service-account workload identity — no code change.

Every required setting is validated at startup. Missing values raise
``ConfigError`` rather than producing a partial config that fails later.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError


def _require(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise ConfigError(f"required environment variable missing: {name}", name=name)
    return value


def _optional(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True, slots=True)
class S3Config:
    endpoint_url: str
    region: str
    bucket: str
    writer_access_key: str
    writer_secret_key: str
    reader_access_key: str
    reader_secret_key: str
    admin_access_key: str
    admin_secret_key: str


@dataclass(frozen=True, slots=True)
class PostgresConfig:
    host: str
    port: int
    database: str
    writer_user: str
    writer_password: str
    reader_user: str
    reader_password: str


@dataclass(frozen=True, slots=True)
class SigningConfig:
    private_key_path: Path
    public_key_path: Path
    key_id: str


@dataclass(frozen=True, slots=True)
class EvidenceConfig:
    s3: S3Config
    postgres: PostgresConfig
    signing: SigningConfig
    retention_class: str
    audit_log_path: Path

    @classmethod
    def from_env(cls) -> "EvidenceConfig":
        try:
            return cls(
                s3=S3Config(
                    endpoint_url=_optional("S3_ENDPOINT_URL", "http://127.0.0.1:9000"),
                    region=_optional("S3_REGION", "us-east-1"),
                    bucket=_require("EVIDENCE_BUCKET"),
                    writer_access_key=_require("EVIDENCE_WRITER_ACCESS_KEY"),
                    writer_secret_key=_require("EVIDENCE_WRITER_SECRET_KEY"),
                    reader_access_key=_require("EVIDENCE_READER_ACCESS_KEY"),
                    reader_secret_key=_require("EVIDENCE_READER_SECRET_KEY"),
                    admin_access_key=_require("EVIDENCE_ADMIN_ACCESS_KEY"),
                    admin_secret_key=_require("EVIDENCE_ADMIN_SECRET_KEY"),
                ),
                postgres=PostgresConfig(
                    host=_optional("POSTGRES_HOST", "127.0.0.1"),
                    port=int(_optional("POSTGRES_PORT", "5432")),
                    database=_require("POSTGRES_DB"),
                    writer_user=_require("POSTGRES_WRITER_USER"),
                    writer_password=_require("POSTGRES_WRITER_PASSWORD"),
                    reader_user=_require("POSTGRES_READER_USER"),
                    reader_password=_require("POSTGRES_READER_PASSWORD"),
                ),
                signing=SigningConfig(
                    private_key_path=Path(_require("SIGNING_PRIVATE_KEY_PATH")),
                    public_key_path=Path(_require("SIGNING_PUBLIC_KEY_PATH")),
                    key_id=_require("SIGNING_KEY_ID"),
                ),
                retention_class=_optional("EVIDENCE_RETENTION_CLASS", "demo_24h"),
                audit_log_path=Path(_optional("AUDIT_LOG_PATH", "./logs/agent_audit.jsonl")),
            )
        except ValueError as exc:
            # Catches int() conversion for POSTGRES_PORT.
            raise ConfigError("invalid configuration value", original=str(exc)) from exc
