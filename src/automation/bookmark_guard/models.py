from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal


@dataclass(frozen=True)
class BookmarkMatch:
    profile_dir: str            # e.g. "Default", "Profile 1"
    chrome_email: str | None    # signed-in account email from Preferences
    url: str
    title: str | None
    item_type: Literal["bookmark", "homepage"]
    pattern_name: str           # which detection rule matched


@dataclass(frozen=True)
class ScanResult:
    scan_id: uuid.UUID
    hostname: str
    os_username: str
    scanned_at_utc: datetime
    matches: tuple[BookmarkMatch, ...]

    @classmethod
    def empty(cls, hostname: str, os_username: str) -> ScanResult:
        return cls(
            scan_id=uuid.uuid4(),
            hostname=hostname,
            os_username=os_username,
            scanned_at_utc=datetime.now(timezone.utc),
            matches=(),
        )


@dataclass(frozen=True)
class RemovalOutcome:
    match: BookmarkMatch
    action_taken: Literal["removed", "removed_by_extension", "failed", "skipped"]
    action_error: str | None = None
    evidence_artefact_id: str | None = None  # UUID of preserved Bookmarks snapshot


@dataclass(frozen=True)
class NotificationOutcome:
    chrome_email: str
    notified_at_utc: datetime | None
    error: str | None = None
