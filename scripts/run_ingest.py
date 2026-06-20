#!/usr/bin/env python3
"""CLI entry point for the ingest stage.

Fetches Google Chat history (spaces + PoI DMs) since the last successful run
and writes raw events to OpenSearch with signed evidence artefacts in MinIO.

Usage:
    python scripts/run_ingest.py [OPTIONS]

Options:
    --config PATH       Path to pipeline.yml (default: config/pipeline.yml)
    --start DATETIME    Watermark start, ISO 8601 UTC. Overrides the last
                        successful run's watermark_end. Default: auto.
    --end DATETIME      Watermark end, ISO 8601 UTC. Default: now().
    --dry-run           Fetch and log events; skip OpenSearch writes and
                        evidence recording. Useful for testing credentials.
    --verbose           Enable debug-level logging.
    --help              Show this message and exit.

Exit codes:
    0   Success (dead-lettered items are logged but do not fail the run).
    1   Connector or pipeline error.
    2   Auth error (check DWD scopes and service account key).
    3   Config error (check pipeline.yml and env vars).
    130 Interrupted (Ctrl-C).

Must be run inside the project virtual environment:
    source venv/bin/activate
    python scripts/run_ingest.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

try:
    from dotenv import load_dotenv  # type: ignore[import]
    load_dotenv(_PROJECT_ROOT / ".env")
except ImportError:
    pass


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    # Use the evidence module's JSON formatter so log output is consistent.
    # Must be imported after sys.path is patched above.
    from evidence.logging_config import configure_logging
    configure_logging(level)


def _parse_datetime(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid datetime {value!r} — expected ISO 8601 UTC, "
            "e.g. 2026-06-20T00:00:00Z"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest Google Chat history into the insider threat pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_PROJECT_ROOT / "config" / "pipeline.yml",
        help="Path to pipeline.yml (default: config/pipeline.yml)",
    )
    parser.add_argument(
        "--start",
        type=_parse_datetime,
        default=None,
        metavar="DATETIME",
        help="Watermark start, ISO 8601 UTC (default: last successful run or 24h ago)",
    )
    parser.add_argument(
        "--end",
        type=_parse_datetime,
        default=None,
        metavar="DATETIME",
        help="Watermark end, ISO 8601 UTC (default: now())",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch events without writing to OpenSearch or recording evidence.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug-level logging.",
    )
    args = parser.parse_args()

    _configure_logging(args.verbose)
    logger = logging.getLogger("ingest")

    from ingest.runner import run
    from ingest.errors import ConnectorAuthError, ConnectorError, ConfigError

    try:
        result = run(
            args.config,
            start=args.start,
            end=args.end,
            dry_run=args.dry_run,
        )
    except ConfigError as exc:
        logger.error("config error: %s", exc)
        return 3
    except ConnectorAuthError as exc:
        logger.error(
            "auth error — check DWD scopes and GOOGLE_SERVICE_ACCOUNT_KEY_PATH: %s", exc
        )
        return 2
    except ConnectorError as exc:
        logger.error("connector error: %s", exc)
        return 1
    except KeyboardInterrupt:
        logger.info("interrupted")
        return 130
    except Exception as exc:
        logger.error("unexpected error: %s", exc, exc_info=True)
        return 1

    if result.status == "failed":
        logger.error(
            "ingest failed — check logs above for details. "
            "Common causes: DWD scopes not configured in Google Workspace Admin, "
            "or service account key path incorrect."
        )
        return 1

    mode = "[DRY RUN] " if args.dry_run else ""
    print(
        f"\n  {mode}Done — {result.records_out} events written. "
        f"Window: {result.watermark_start:%Y-%m-%dT%H:%M:%SZ} → "
        f"{result.watermark_end:%Y-%m-%dT%H:%M:%SZ}\n"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
