from __future__ import annotations


class BookmarkGuardError(Exception):
    """Base class for all bookmark_guard errors."""


class UnsupportedPlatformError(BookmarkGuardError):
    """Script was run on a non-macOS platform."""


class MacOSVersionError(BookmarkGuardError):
    """macOS version does not meet the minimum requirement."""


class ChromeRunningError(BookmarkGuardError):
    """Chrome is running; safe modification of bookmark files is not possible."""


class BookmarkReadError(BookmarkGuardError):
    """Could not read or parse a Chrome bookmark/preferences file."""


class BookmarkWriteError(BookmarkGuardError):
    """Could not write back a modified Chrome bookmark/preferences file."""


class NotificationError(BookmarkGuardError):
    """Google Chat notification failed."""


class ConfigError(BookmarkGuardError):
    """Configuration is missing or invalid."""
