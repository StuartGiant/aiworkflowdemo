"""Structured JSON logging, UTC, with light secret/PII redaction.

Per project rules: structured JSON, UTC, no secrets/PII, redact on output.

This is a deliberately small implementation rather than a dependency on a
heavier framework. The redaction pass is best-effort and not a substitute for
not passing secrets in the first place — see ``REDACT_KEYS``.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Mapping

REDACT_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "secret",
        "secret_key",
        "secret_access_key",
        "access_key",
        "token",
        "authorization",
        "api_key",
        "private_key",
        "cookie",
    }
)
REDACTED = "[REDACTED]"


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            k: (REDACTED if k.lower() in REDACT_KEYS else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Extra fields attached via logger.info("...", extra={"context": {...}})
        extra = getattr(record, "context", None)
        if extra is not None:
            payload["context"] = _redact(extra)
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logging(level: int = logging.INFO) -> None:
    """Idempotent global logging setup. Safe to call multiple times."""
    root = logging.getLogger()
    if getattr(root, "_evidence_configured", False):
        return
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers[:] = [handler]
    root.setLevel(level)
    root._evidence_configured = True  # type: ignore[attr-defined]


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)
