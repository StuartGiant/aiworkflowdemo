"""Ingest-stage exception hierarchy — ADR 0004."""

from __future__ import annotations

from typing import Any


class ConnectorError(Exception):
    """Base class for all connector errors."""

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = dict(context)


class ConnectorAuthError(ConnectorError):
    """Authentication or authorisation failure — dead-letter immediately, no retry."""


class ConnectorRateLimitError(ConnectorError):
    """API rate limit hit — caller should sleep retry_after seconds then retry."""

    def __init__(self, message: str, *, retry_after: float = 60.0, **context: Any) -> None:
        super().__init__(message, **context)
        self.retry_after = retry_after


class ConnectorTransientError(ConnectorError):
    """Transient network or server error — caller retries with exponential back-off."""


class ConfigError(ConnectorError):
    """Pipeline configuration is missing or invalid — raised at startup."""
