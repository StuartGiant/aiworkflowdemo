"""runner.py — orchestrates detect → respond → notify for bookmark_guard."""

from __future__ import annotations

import logging
from pathlib import Path

from .config import BookmarkGuardConfig
from .detector import ChromeBookmarkDetector
from .models import NotificationOutcome, RemovalOutcome, ScanResult
from .notifier import GmailNotifier
from .responder import BookmarkResponder

logger = logging.getLogger(__name__)


def run(config_path: Path, *, dry_run: bool = False) -> None:
    """Run the full bookmark-guard automation.

    1. Detect — scan Chrome bookmarks and homepage for sensitive URLs.
    2. Respond — remove matching items; write violation records to DB.
    3. Notify — send a Google Chat DM to each affected employee.

    Raises BookmarkGuardError (or subclass) on unrecoverable failure.
    """
    config = BookmarkGuardConfig.from_file(config_path, dry_run=dry_run)

    logger.info(
        "bookmark_guard.runner.start",
        extra={
            "config_path": str(config_path),
            "dry_run": dry_run,
            "patterns": [p.name for p in config.patterns],
        },
    )

    # --- 1. Detect -----------------------------------------------------------
    detector = ChromeBookmarkDetector(config.patterns, config.corporate_email_domain)
    result: ScanResult = detector.scan()

    if not result.matches:
        logger.info(
            "bookmark_guard.runner.clean",
            extra={"hostname": result.hostname, "os_username": result.os_username},
        )
        print(f"\n  No sensitive bookmarks or homepages found on {result.hostname}.\n")
        return

    logger.info(
        "bookmark_guard.runner.violations_found",
        extra={
            "hostname": result.hostname,
            "os_username": result.os_username,
            "count": len(result.matches),
        },
    )
    _print_violations(result)

    # --- 2. Respond ----------------------------------------------------------
    responder = BookmarkResponder(config)
    outcomes: list[RemovalOutcome] = responder.respond(result)

    removed = sum(1 for o in outcomes if o.action_taken == "removed")
    failed = sum(1 for o in outcomes if o.action_taken == "failed")
    skipped = sum(1 for o in outcomes if o.action_taken == "skipped")

    logger.info(
        "bookmark_guard.runner.response_complete",
        extra={"removed": removed, "failed": failed, "skipped": skipped},
    )

    # --- 3. Notify -----------------------------------------------------------
    notifier = GmailNotifier(config)
    notification_outcomes: list[NotificationOutcome] = notifier.notify(result, outcomes)

    notified = sum(1 for n in notification_outcomes if n.notified_at_utc is not None)
    notify_failed = sum(1 for n in notification_outcomes if n.notified_at_utc is None)

    logger.info(
        "bookmark_guard.runner.complete",
        extra={
            "violations": len(result.matches),
            "removed": removed,
            "failed": failed,
            "notified": notified,
            "notify_failed": notify_failed,
        },
    )

    if dry_run:
        print(
            f"\n  DRY RUN complete — {len(result.matches)} violation(s) detected, "
            "no changes made.\n"
        )
    else:
        print(
            f"\n  Done — {removed} removed, {failed} failed, "
            f"{notified} employee(s) notified.\n"
        )


def _print_violations(result: ScanResult) -> None:
    print()
    print(f"  {'─' * 70}")
    print(f"  DETECTED VIOLATIONS  —  host: {result.hostname}  "
          f"os_user: {result.os_username}")
    print(f"  {'─' * 70}")

    for i, m in enumerate(result.matches, 1):
        print(f"\n  [{i}] {m.item_type.upper()}")
        print(f"      Profile  : {m.profile_dir}")
        print(f"      Account  : {m.chrome_email or '(unknown)'}")
        print(f"      Pattern  : {m.pattern_name}")
        print(f"      Title    : {m.title or '(no title)'}")
        print(f"      URL      : {m.url}")

    print(f"\n  {'─' * 70}")
    print(f"  Total: {len(result.matches)} violation(s)")
    print(f"  {'─' * 70}")
    print()
