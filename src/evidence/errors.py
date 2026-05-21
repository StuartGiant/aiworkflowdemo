"""Exception hierarchy for the evidence module.

A single base class makes it easy for callers to catch evidence-related failures
without resorting to bare except. Every error carries a structured ``context``
dict that gets logged alongside the message (with PII/secret redaction handled
by ``logging_config``).
"""

from __future__ import annotations

from typing import Any


class EvidenceError(Exception):
    """Base class for all evidence module errors."""

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = dict(context)

    def __str__(self) -> str:  # pragma: no cover - trivial
        if not self.context:
            return self.message
        return f"{self.message} :: {self.context}"


class ConfigError(EvidenceError):
    """Raised when required configuration is missing or invalid."""


class StorageError(EvidenceError):
    """Raised for object-store interactions (upload, download, retention)."""


class SignatureError(EvidenceError):
    """Raised when a manifest signature cannot be created or verified."""


class CustodyError(EvidenceError):
    """Raised for custody-ledger problems (chain break, DB error, role error)."""


class IntegrityError(EvidenceError):
    """Raised when verify_evidence detects tampering or corruption."""


class ValidationError(EvidenceError):
    """Raised when input to record_evidence fails validation."""
