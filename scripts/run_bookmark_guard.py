#!/usr/bin/env python3
"""CLI entry point for the bookmark_guard automation.

Usage:
    python scripts/run_bookmark_guard.py [--config PATH] [--dry-run]

Options:
    --config PATH   Path to bookmark_guard.yml  (default: config/bookmark_guard.yml)
    --dry-run       Scan and log violations without modifying files, writing to DB,
                    or sending notifications.
    --help          Show this message and exit.

Must be run inside the project virtual environment:
    source venv/bin/activate
    python scripts/run_bookmark_guard.py

Requires macOS >= 26.0.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Resolve project root so src/ is importable regardless of cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

# Load .env before importing project modules so env vars are available.
try:
    from dotenv import load_dotenv  # type: ignore[import]
    load_dotenv(_PROJECT_ROOT / ".env")
except ImportError:
    pass  # dotenv is optional; env vars may be set by other means


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        stream=sys.stdout,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Detect and remove sensitive Chrome bookmarks/homepage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_PROJECT_ROOT / "config" / "bookmark_guard.yml",
        help="Path to bookmark_guard.yml (default: config/bookmark_guard.yml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan only — do not modify files, write to DB, or send notifications.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug-level logging.",
    )
    args = parser.parse_args()

    _configure_logging(args.verbose)
    logger = logging.getLogger("bookmark_guard")

    from automation.bookmark_guard import run
    from automation.bookmark_guard.errors import (
        BookmarkGuardError,
        ChromeRunningError,
        MacOSVersionError,
        UnsupportedPlatformError,
    )

    try:
        run(args.config, dry_run=args.dry_run)
        return 0
    except UnsupportedPlatformError as exc:
        logger.error("platform not supported: %s", exc)
        return 2
    except MacOSVersionError as exc:
        logger.error("macOS version requirement not met: %s", exc)
        return 2
    except ChromeRunningError as exc:
        logger.error("chrome is running — close Chrome and retry: %s", exc)
        return 3
    except BookmarkGuardError as exc:
        logger.error("bookmark_guard error: %s", exc)
        return 1
    except KeyboardInterrupt:
        logger.info("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
