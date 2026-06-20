"""Google Chat connector — ADR 0004.

Fetches Chat messages from two source sets:
  * All non-DM spaces (SPACE + GROUP_CHAT types) — via admin-scoped DWD.
  * DM spaces for configured PoI subjects — via per-user DWD impersonation,
    then admin-scoped message reads.

Each yielded RawEvent carries the full Chat API message object. For DM spaces,
the raw_json is enriched with a ``_space_members`` key containing both parties'
email and display name (fetched once per space via the members API).
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ..errors import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorRateLimitError,
    ConnectorTransientError,
    ConfigError,
)
from ..protocol import HealthStatus, RawEvent

log = logging.getLogger(__name__)

_ADMIN_SCOPES = [
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.memberships.readonly",
    "https://www.googleapis.com/auth/chat.messages.readonly",
    "https://www.googleapis.com/auth/chat.admin.spaces.readonly",
    "https://www.googleapis.com/auth/chat.admin.memberships.readonly",
]

_USER_SCOPES = [
    "https://www.googleapis.com/auth/chat.spaces.readonly",
]

_SPACE_TYPES = ("SPACE", "GROUP_CHAT")


@dataclass(frozen=True, slots=True)
class GoogleChatConnectorConfig:
    service_account_key_path: Path
    admin_email: str
    poi_emails: tuple[str, ...]
    spaces_enabled: bool
    dms_enabled: bool
    page_size: int
    inter_page_delay_ms: int
    http_timeout_seconds: int
    max_retries: int
    enabled: bool


class GoogleChatConnector:
    source_system = "google_workspace.chat"

    def __init__(self, config: GoogleChatConnectorConfig) -> None:
        self._config = config
        self._admin_svc = self._build_chat_service(
            self._build_admin_credentials()
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch(
        self, *, start: datetime, end: datetime, run_id: uuid.UUID
    ) -> Iterator[RawEvent]:
        if not self._config.enabled:
            return

        if self._config.spaces_enabled:
            for space_type in _SPACE_TYPES:
                for space in self._list_spaces(space_type):
                    space_name = space["name"]
                    log.debug(
                        "ingest.chat.fetch_space",
                        extra={"context": {"space": space_name, "type": space_type}},
                    )
                    yield from self._fetch_messages(space_name, start, end)

        if self._config.dms_enabled:
            for email in self._config.poi_emails:
                for space in self._list_dm_spaces_for_user(email):
                    space_name = space["name"]
                    members = self._list_space_members(space_name)
                    log.debug(
                        "ingest.chat.fetch_dm",
                        extra={"context": {
                            "space": space_name,
                            "poi": email,
                            "members": [m.get("member", {}).get("email") for m in members],
                        }},
                    )
                    yield from self._fetch_messages(
                        space_name, start, end, space_members=members
                    )

    def health_check(self) -> HealthStatus:
        t0 = time.monotonic()
        try:
            self._admin_svc.spaces().list(
                filter='spaceType = "SPACE"',
                pageSize=1,
            ).execute()
            latency = (time.monotonic() - t0) * 1000
            return HealthStatus(healthy=True, latency_ms=latency)
        except HttpError as exc:
            mapped = _map_http_error(exc, context="health_check")
            log.warning(
                "ingest.chat.health_check_failed",
                extra={"context": {"err": str(mapped)}},
            )
            return HealthStatus(
                healthy=False,
                latency_ms=(time.monotonic() - t0) * 1000,
                detail=str(mapped),
            )
        except Exception as exc:
            log.warning(
                "ingest.chat.health_check_failed",
                extra={"context": {"err": str(exc)}},
            )
            return HealthStatus(
                healthy=False,
                latency_ms=(time.monotonic() - t0) * 1000,
                detail=str(exc),
            )

    # ------------------------------------------------------------------
    # Space and member discovery
    # ------------------------------------------------------------------

    def _list_spaces(self, space_type: str) -> Iterator[dict]:
        page_token = None
        while True:
            try:
                resp = self._admin_svc.spaces().list(
                    filter=f'spaceType = "{space_type}"',
                    pageSize=min(self._config.page_size, 1000),
                    pageToken=page_token,
                ).execute()
            except HttpError as exc:
                raise _map_http_error(exc, context=f"list {space_type} spaces") from exc

            for space in resp.get("spaces", []):
                yield space

            page_token = resp.get("nextPageToken")
            if not page_token:
                break
            time.sleep(self._config.inter_page_delay_ms / 1000)

    def _list_dm_spaces_for_user(self, user_email: str) -> Iterator[dict]:
        try:
            user_svc = self._build_chat_service(
                self._build_user_credentials(user_email)
            )
        except Exception as exc:
            log.warning(
                "ingest.chat.dm_user_auth_failed",
                extra={"context": {"user": user_email, "err": str(exc)}},
            )
            return

        page_token = None
        while True:
            try:
                resp = user_svc.spaces().list(
                    filter='spaceType = "DIRECT_MESSAGE"',
                    pageSize=100,
                    pageToken=page_token,
                ).execute()
            except HttpError as exc:
                mapped = _map_http_error(exc, context=f"list DM spaces for {user_email}")
                if isinstance(mapped, ConnectorAuthError):
                    log.warning(
                        "ingest.chat.dm_user_auth_failed",
                        extra={"context": {"user": user_email, "err": str(mapped)}},
                    )
                    return
                raise mapped from exc

            for space in resp.get("spaces", []):
                yield space

            page_token = resp.get("nextPageToken")
            if not page_token:
                break
            time.sleep(self._config.inter_page_delay_ms / 1000)

    def _list_space_members(self, space_name: str) -> list[dict]:
        members: list[dict] = []
        page_token = None
        while True:
            try:
                resp = self._admin_svc.spaces().members().list(
                    parent=space_name,
                    pageSize=100,
                    pageToken=page_token,
                ).execute()
            except HttpError as exc:
                raise _map_http_error(exc, context=f"list members of {space_name}") from exc

            members.extend(resp.get("memberships", []))

            page_token = resp.get("nextPageToken")
            if not page_token:
                break
            time.sleep(self._config.inter_page_delay_ms / 1000)

        return members

    # ------------------------------------------------------------------
    # Message fetching
    # ------------------------------------------------------------------

    def _fetch_messages(
        self,
        space_name: str,
        start: datetime,
        end: datetime,
        *,
        space_members: list[dict] | None = None,
    ) -> Iterator[RawEvent]:
        start_ts = start.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        end_ts = end.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        msg_filter = f'createTime > "{start_ts}" AND createTime < "{end_ts}"'

        page_token = None
        while True:
            try:
                resp = self._admin_svc.spaces().messages().list(
                    parent=space_name,
                    filter=msg_filter,
                    pageSize=min(self._config.page_size, 1000),
                    pageToken=page_token,
                ).execute()
            except HttpError as exc:
                raise _map_http_error(
                    exc, context=f"list messages in {space_name}"
                ) from exc

            for msg in resp.get("messages", []):
                raw_json: dict = dict(msg)
                if space_members is not None:
                    raw_json["_space_members"] = [
                        {
                            "email": m.get("member", {}).get("email"),
                            "displayName": m.get("member", {}).get("displayName"),
                            "type": m.get("member", {}).get("type"),
                        }
                        for m in space_members
                    ]

                occurred_at = _parse_create_time(msg["createTime"])
                yield RawEvent(
                    source_system=self.source_system,
                    event_id=msg["name"],
                    occurred_at_utc=occurred_at,
                    original_timezone="+00:00",
                    raw_json=raw_json,
                )

            page_token = resp.get("nextPageToken")
            if not page_token:
                break
            time.sleep(self._config.inter_page_delay_ms / 1000)

    # ------------------------------------------------------------------
    # Credential and service builders
    # ------------------------------------------------------------------

    def _build_admin_credentials(self) -> service_account.Credentials:
        key_path = str(self._config.service_account_key_path)
        if not Path(key_path).exists():
            raise ConfigError(
                f"service account key not found: {key_path}",
                path=key_path,
            )
        return (
            service_account.Credentials.from_service_account_file(
                key_path, scopes=_ADMIN_SCOPES
            ).with_subject(self._config.admin_email)
        )

    def _build_user_credentials(self, user_email: str) -> service_account.Credentials:
        key_path = str(self._config.service_account_key_path)
        return (
            service_account.Credentials.from_service_account_file(
                key_path, scopes=_USER_SCOPES
            ).with_subject(user_email)
        )

    def _build_chat_service(self, credentials: service_account.Credentials):
        return build("chat", "v1", credentials=credentials, cache_discovery=False)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _parse_create_time(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _map_http_error(exc: HttpError, *, context: str) -> ConnectorError:
    status = int(exc.resp.status)
    body = exc.content.decode("utf-8", errors="replace") if exc.content else ""

    if status == 429 or (status == 403 and "rateLimitExceeded" in body):
        return ConnectorRateLimitError(
            f"{context}: rate limit ({status})", retry_after=60.0, http_status=status
        )
    if status in (401, 403):
        return ConnectorAuthError(
            f"{context}: auth error ({status})", http_status=status, body=body[:200]
        )
    if status in (500, 502, 503, 504):
        return ConnectorTransientError(
            f"{context}: server error ({status})", http_status=status
        )
    return ConnectorError(
        f"{context}: unexpected HTTP {status}", http_status=status, body=body[:200]
    )


def config_from_yaml(raw: dict) -> GoogleChatConnectorConfig:
    """Build GoogleChatConnectorConfig from the parsed pipeline.yml connectors.google_chat dict."""
    import os

    key_path = Path(
        os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY_PATH", "")
        or raw.get("service_account_key_path", "")
    )
    if not key_path or str(key_path) in ("", "."):
        raise ConfigError(
            "GOOGLE_SERVICE_ACCOUNT_KEY_PATH env var or "
            "connectors.google_chat.service_account_key_path is required"
        )

    admin_email = (
        os.environ.get("GOOGLE_WORKSPACE_ADMIN_EMAIL", "")
        or raw.get("admin_email", "")
    )
    if not admin_email:
        raise ConfigError(
            "GOOGLE_WORKSPACE_ADMIN_EMAIL env var or "
            "connectors.google_chat.admin_email is required"
        )

    dms_raw = raw.get("dms", {})
    poi_emails = tuple(str(e) for e in dms_raw.get("poi_emails", []))

    spaces_raw = raw.get("spaces", {})

    return GoogleChatConnectorConfig(
        service_account_key_path=key_path,
        admin_email=admin_email,
        poi_emails=poi_emails,
        spaces_enabled=bool(spaces_raw.get("enabled", True)),
        dms_enabled=bool(dms_raw.get("enabled", True)),
        page_size=int(raw.get("page_size", 1000)),
        inter_page_delay_ms=int(raw.get("inter_page_delay_ms", 200)),
        http_timeout_seconds=int(raw.get("http_timeout_seconds", 30)),
        max_retries=int(raw.get("max_retries", 3)),
        enabled=bool(raw.get("enabled", True)),
    )
