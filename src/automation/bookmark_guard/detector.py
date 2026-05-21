"""detector.py — reads Chrome bookmarks and homepage; identifies sensitive URLs.

Supports macOS >= 26.0 only.  Reads all Chrome profiles for the current OS
user without modifying any files.
"""

from __future__ import annotations

import getpass
import json
import logging
import platform
import socket
import uuid
from collections.abc import Generator
from datetime import datetime, timezone
from pathlib import Path

from .config import SensitivePattern
from .errors import (
    BookmarkReadError,
    MacOSVersionError,
    UnsupportedPlatformError,
)
from .models import BookmarkMatch, ScanResult

logger = logging.getLogger(__name__)

_MIN_MACOS: tuple[int, int] = (26, 0)
_CHROME_DATA_DIR = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
_PROFILE_ROOT_KEYS = ("bookmark_bar", "other", "synced")


class ChromeBookmarkDetector:
    def __init__(
        self,
        patterns: tuple[SensitivePattern, ...],
        corporate_email_domain: str,
    ) -> None:
        if not patterns:
            raise ValueError("at least one detection pattern is required")
        if not corporate_email_domain:
            raise ValueError("corporate_email_domain is required")
        self._patterns = patterns
        # Normalise: strip leading '@' and lowercase for comparison.
        self._corp_domain = corporate_email_domain.lower().lstrip("@")

    # ------------------------------------------------------------------ public

    def scan(self) -> ScanResult:
        _assert_macos_version()
        hostname = socket.gethostname()
        os_username = getpass.getuser()

        logger.info(
            "bookmark_guard.detector.scan.start",
            extra={"hostname": hostname, "os_username": os_username},
        )

        matches: list[BookmarkMatch] = []
        for profile_dir, profile_name in _iter_profile_dirs():
            prefs = _load_preferences(profile_dir)
            chrome_email = _get_chrome_email(prefs)

            if not _is_corporate_profile(chrome_email, self._corp_domain):
                logger.info(
                    "bookmark_guard.detector.private_profile_skipped",
                    extra={"profile": profile_name, "chrome_email": chrome_email or "<none>"},
                )
                continue

            logger.debug(
                "bookmark_guard.detector.scanning_profile",
                extra={"profile": profile_name, "chrome_email": chrome_email},
            )

            bm_file = profile_dir / "Bookmarks"
            if bm_file.exists():
                try:
                    matches.extend(
                        _scan_bookmarks(bm_file, profile_name, chrome_email, self._patterns)
                    )
                except (json.JSONDecodeError, OSError) as exc:
                    raise BookmarkReadError(
                        f"could not read bookmarks file: {bm_file}"
                    ) from exc

            hp_match = _scan_homepage(prefs, profile_name, chrome_email, self._patterns)
            if hp_match:
                matches.append(hp_match)

        result = ScanResult(
            scan_id=uuid.uuid4(),
            hostname=hostname,
            os_username=os_username,
            scanned_at_utc=datetime.now(timezone.utc),
            matches=tuple(matches),
        )

        logger.info(
            "bookmark_guard.detector.scan.complete",
            extra={"matches": len(matches)},
        )
        return result


# --------------------------------------------------------------- macos check

def _assert_macos_version() -> None:
    if platform.system() != "Darwin":
        raise UnsupportedPlatformError(
            f"bookmark_guard requires macOS; current platform: {platform.system()!r}"
        )
    ver_str = platform.mac_ver()[0]
    if not ver_str:
        raise MacOSVersionError("could not determine macOS version")

    parts = ver_str.split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
    except ValueError as exc:
        raise MacOSVersionError(
            f"unparseable macOS version string: {ver_str!r}"
        ) from exc

    if (major, minor) < _MIN_MACOS:
        raise MacOSVersionError(
            f"bookmark_guard requires macOS {_MIN_MACOS[0]}.{_MIN_MACOS[1]} "
            f"or later; detected version: {ver_str}"
        )


# ----------------------------------------------------------- profile helpers

def _is_corporate_profile(chrome_email: str | None, corp_domain: str) -> bool:
    """Return True only if the profile is signed in with a corporate email."""
    if not chrome_email:
        return False
    return chrome_email.lower().endswith(f"@{corp_domain}")


def _iter_profile_dirs() -> Generator[tuple[Path, str], None, None]:
    if not _CHROME_DATA_DIR.exists():
        logger.warning(
            "bookmark_guard.detector.no_chrome_dir",
            extra={"path": str(_CHROME_DATA_DIR)},
        )
        return

    for candidate in sorted(_CHROME_DATA_DIR.iterdir()):
        if not candidate.is_dir():
            continue
        name = candidate.name
        if name != "Default" and not name.startswith("Profile "):
            continue
        if (candidate / "Bookmarks").exists() or (candidate / "Preferences").exists():
            yield candidate, name


def _load_preferences(profile_dir: Path) -> dict:
    prefs_file = profile_dir / "Preferences"
    if not prefs_file.exists():
        return {}
    try:
        with prefs_file.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def _get_chrome_email(prefs: dict) -> str | None:
    accounts = prefs.get("account_info")
    if isinstance(accounts, list) and accounts:
        email = accounts[0].get("email")
        if isinstance(email, str) and email:
            return email
    return None


# ------------------------------------------------------- bookmark scanning

def _scan_bookmarks(
    bm_file: Path,
    profile_name: str,
    chrome_email: str | None,
    patterns: tuple[SensitivePattern, ...],
) -> list[BookmarkMatch]:
    with bm_file.open(encoding="utf-8") as fh:
        data = json.load(fh)

    matches: list[BookmarkMatch] = []
    roots = data.get("roots", {})
    for root_key in _PROFILE_ROOT_KEYS:
        root_node = roots.get(root_key)
        if isinstance(root_node, dict):
            matches.extend(
                _walk_node(root_node, profile_name, chrome_email, patterns)
            )
    return matches


def _walk_node(
    node: dict,
    profile_name: str,
    chrome_email: str | None,
    patterns: tuple[SensitivePattern, ...],
) -> Generator[BookmarkMatch, None, None]:
    node_type = node.get("type")

    if node_type == "url":
        url = node.get("url", "")
        for pat in patterns:
            if pat.pattern.search(url):
                yield BookmarkMatch(
                    profile_dir=profile_name,
                    chrome_email=chrome_email,
                    url=url,
                    title=node.get("name") or None,
                    item_type="bookmark",
                    pattern_name=pat.name,
                )
                break  # one match per bookmark; first pattern wins

    elif node_type == "folder":
        for child in node.get("children", []):
            if isinstance(child, dict):
                yield from _walk_node(child, profile_name, chrome_email, patterns)


# ------------------------------------------------------- homepage scanning

def _scan_homepage(
    prefs: dict,
    profile_name: str,
    chrome_email: str | None,
    patterns: tuple[SensitivePattern, ...],
) -> BookmarkMatch | None:
    if prefs.get("homepage_is_newtabpage", True):
        return None

    url = prefs.get("homepage", "")
    if not isinstance(url, str) or not url:
        return None

    for pat in patterns:
        if pat.pattern.search(url):
            return BookmarkMatch(
                profile_dir=profile_name,
                chrome_email=chrome_email,
                url=url,
                title="Homepage",
                item_type="homepage",
                pattern_name=pat.name,
            )
    return None
