"""Connector protocol — ADR 0004.

Every source connector must satisfy ConnectorProtocol. The runner calls
fetch() to iterate raw events and health_check() before each run.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Protocol, runtime_checkable


@dataclass(frozen=True)
class RawEvent:
    source_system: str
    event_id: str
    occurred_at_utc: datetime
    original_timezone: str
    raw_json: dict
    collected_at_utc: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        canonical = json.dumps(
            self.raw_json, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        object.__setattr__(self, "sha256", hashlib.sha256(canonical).hexdigest())


@dataclass(frozen=True)
class HealthStatus:
    healthy: bool
    latency_ms: float
    detail: str | None = None


@runtime_checkable
class ConnectorProtocol(Protocol):
    source_system: str

    def fetch(
        self, *, start: datetime, end: datetime, run_id: uuid.UUID
    ) -> Iterator[RawEvent]: ...

    def health_check(self) -> HealthStatus: ...
