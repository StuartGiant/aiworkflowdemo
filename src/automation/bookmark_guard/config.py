from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .errors import ConfigError


@dataclass(frozen=True, slots=True)
class PostgresConfig:
    host: str
    port: int
    database: str
    writer_user: str
    writer_password: str
    reader_user: str
    reader_password: str


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"required environment variable missing: {name}")
    return value


def _optional_env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value else default


@dataclass(frozen=True, slots=True)
class SensitivePattern:
    name: str
    pattern: re.Pattern[str]
    description: str = ""


@dataclass(frozen=True, slots=True)
class NotificationConfig:
    service_account_key_path: Path
    sender_email: str       # Gmail address the service account impersonates via DWD
    message_template: str


@dataclass(frozen=True, slots=True)
class BookmarkGuardConfig:
    patterns: tuple[SensitivePattern, ...]
    postgres: PostgresConfig
    notification: NotificationConfig
    corporate_email_domain: str   # e.g. "zeroinsiderai.com" — private profiles skipped
    stop_if_chrome_running: bool
    dry_run: bool

    @classmethod
    def from_file(cls, config_path: Path, *, dry_run: bool = False) -> BookmarkGuardConfig:
        if not config_path.exists():
            raise ConfigError(f"config file not found: {config_path}")

        with config_path.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)

        if not isinstance(raw, dict):
            raise ConfigError(f"config file is not a YAML mapping: {config_path}")

        patterns = _load_patterns(raw.get("patterns", []))
        notification = _load_notification(raw.get("notification", {}))
        options = raw.get("options", {})

        domain = options.get("corporate_email_domain", "")
        if not domain:
            raise ConfigError(
                "options.corporate_email_domain is required "
                "(e.g. 'zeroinsiderai.com')"
            )

        postgres = _load_postgres()

        return cls(
            patterns=patterns,
            postgres=postgres,
            notification=notification,
            corporate_email_domain=str(domain).lower().lstrip("@"),
            stop_if_chrome_running=bool(options.get("stop_if_chrome_running", False)),
            dry_run=dry_run,
        )


def _load_patterns(raw_patterns: list) -> tuple[SensitivePattern, ...]:
    if not isinstance(raw_patterns, list) or not raw_patterns:
        raise ConfigError("config must contain at least one entry under 'patterns'")

    result: list[SensitivePattern] = []
    for i, entry in enumerate(raw_patterns):
        if not isinstance(entry, dict):
            raise ConfigError(f"patterns[{i}] must be a mapping")
        name = entry.get("name")
        pattern_str = entry.get("pattern")
        if not name:
            raise ConfigError(f"patterns[{i}] is missing 'name'")
        if not pattern_str:
            raise ConfigError(f"patterns[{i}] ({name!r}) is missing 'pattern'")
        try:
            compiled = re.compile(pattern_str, re.IGNORECASE)
        except re.error as exc:
            raise ConfigError(
                f"patterns[{i}] ({name!r}) has invalid regex: {exc}"
            ) from exc
        result.append(
            SensitivePattern(
                name=str(name),
                pattern=compiled,
                description=str(entry.get("description", "")),
            )
        )
    return tuple(result)


def _load_notification(raw: dict) -> NotificationConfig:
    # Key path comes from env var (secret — never in config file).
    key_path = Path(
        _optional_env(
            "GOOGLE_SERVICE_ACCOUNT_KEY_PATH",
            raw.get("service_account_key_path", ""),
        )
    )
    if not key_path or str(key_path) in ("", "."):
        raise ConfigError(
            "notification requires GOOGLE_SERVICE_ACCOUNT_KEY_PATH env var "
            "or notification.service_account_key_path in config"
        )

    # Sender email: read from config YAML (not a secret).
    # Env var GOOGLE_NOTIFICATION_SENDER_EMAIL overrides YAML value if set.
    sender_email = _optional_env(
        "GOOGLE_NOTIFICATION_SENDER_EMAIL",
        raw.get("sender_email", ""),
    )
    if not sender_email:
        raise ConfigError(
            "notification requires notification.sender_email in config "
            "or GOOGLE_NOTIFICATION_SENDER_EMAIL env var"
        )

    template = raw.get("message_template", _DEFAULT_MESSAGE_TEMPLATE)

    return NotificationConfig(
        service_account_key_path=key_path,
        sender_email=sender_email,
        message_template=template,
    )


def _load_postgres() -> PostgresConfig:
    return PostgresConfig(
        host=_optional_env("POSTGRES_HOST", "127.0.0.1"),
        port=int(_optional_env("POSTGRES_PORT", "5432")),
        database=_require_env("POSTGRES_DB"),
        writer_user=_require_env("POSTGRES_WRITER_USER"),
        writer_password=_require_env("POSTGRES_WRITER_PASSWORD"),
        reader_user=_require_env("POSTGRES_READER_USER"),
        reader_password=_require_env("POSTGRES_READER_PASSWORD"),
    )


_DEFAULT_MESSAGE_TEMPLATE = """\
Hi {display_name},

This is an automated security notification from the Cybersecurity team.

We detected {count} bookmark(s) or browser homepage setting(s) on your \
workstation ({hostname}) that contain URLs with sensitive information. \
Those items have been automatically removed from your Chrome browser.

Removed items:
{item_list}

Please do not bookmark or set as your browser homepage any URLs that contain \
sensitive information such as PII, financial records, or internal restricted systems.

If you believe this was done in error, or if you have questions, please \
contact the Cybersecurity team.

Thank you for your cooperation.\
"""
