"""Content moderation pipeline entrypoint.

Usage:
    source venv/bin/activate
    python scripts/run_content_moderation.py

Environment variables required (see .env):
    ANTHROPIC_API_KEY
    GOOGLE_SERVICE_ACCOUNT_KEY_PATH
    GOOGLE_WORKSPACE_ADMIN_EMAIL
    MODERATION_REVIEWER_EMAIL
    MODERATION_REVIEWER_CHAT_USER_ID
    PUBSUB_PROJECT_ID
    PUBSUB_SUBSCRIPTION_ID
    POSTGRES_WRITER_DSN   (or individual POSTGRES_* vars)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path

# Ensure project root is on PYTHONPATH when invoked directly
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()

from src.evidence.logging_config import configure_logging  # noqa: E402
from src.moderation.actions.card_builder import set_interaction_base_url  # noqa: E402
from src.moderation.actions.interaction_handler import create_app  # noqa: E402
from src.moderation.chat_listener import ChatListener  # noqa: E402
from src.moderation.config import load_config  # noqa: E402
from src.moderation.orchestrator import ModerationOrchestrator  # noqa: E402

log = logging.getLogger(__name__)


def _build_db_dsn() -> str:
    """Build a PostgreSQL connection string from environment variables.

    Avoids URL-encoding issues with special characters in passwords by using
    the key=value conninfo format instead of a URI.
    """
    dsn = os.environ.get("POSTGRES_WRITER_DSN", "")
    if dsn:
        return dsn

    host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "aiwf")
    user = os.environ.get("POSTGRES_WRITER_USER", "")
    password = os.environ.get("POSTGRES_WRITER_PASSWORD", "")

    if not user or not password:
        raise EnvironmentError(
            "Database credentials not set. Provide POSTGRES_WRITER_DSN or "
            "POSTGRES_WRITER_USER + POSTGRES_WRITER_PASSWORD."
        )

    # Use conninfo key=value format — safe for passwords containing @ or /
    return f"host={host} port={port} dbname={db} user={user} password={password}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Content moderation Pub/Sub listener")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run detection only — skip message delete, Chat DM, email, and DB writes.",
    )
    args = parser.parse_args()

    configure_logging()
    log.info("moderation.startup")
    if args.dry_run:
        log.info("moderation.startup.dry_run_enabled")

    # Validate required env vars early
    required = [
        "GOOGLE_SERVICE_ACCOUNT_KEY_PATH",
        "GOOGLE_WORKSPACE_ADMIN_EMAIL",
        "PUBSUB_PROJECT_ID",
        "PUBSUB_SUBSCRIPTION_ID",
    ]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        log.error(
            "moderation.startup.missing_env",
            extra={"context": {"missing": missing}},
        )
        sys.exit(1)

    admin_email = os.environ["GOOGLE_WORKSPACE_ADMIN_EMAIL"]
    db_dsn = _build_db_dsn()

    config = load_config()

    orchestrator = ModerationOrchestrator(
        config=config,
        db_dsn=db_dsn,
        admin_email=admin_email,
        dry_run=args.dry_run,
    )

    listener = ChatListener(
        config=config,
        orchestrator=orchestrator,
        admin_email=admin_email,
    )

    # Set the public base URL used to build disposition button links in cards.
    # Must be the publicly reachable URL (e.g. ngrok https URL), not localhost.
    interaction_base_url = os.environ.get("INTERACTION_BASE_URL", "")
    if interaction_base_url:
        set_interaction_base_url(interaction_base_url)
        log.info("moderation.interaction_server.base_url",
                 extra={"context": {"url": interaction_base_url}})
    else:
        log.warning("moderation.interaction_server.no_base_url",
                    extra={"context": {"hint": "Set INTERACTION_BASE_URL to the public ngrok URL"}})

    # Start the interaction server (button click callbacks) in a background thread
    flask_app = create_app(
        db_dsn=db_dsn,
        sa_key_path=config.service_account_key_path,
        admin_email=admin_email,
    )
    srv_cfg = config.interaction_server
    flask_thread = threading.Thread(
        target=lambda: flask_app.run(
            host=srv_cfg.host,
            port=srv_cfg.port,
            debug=False,
            use_reloader=False,
        ),
        daemon=True,
        name="interaction-server",
    )
    flask_thread.start()
    log.info(
        "moderation.interaction_server.started",
        extra={"context": {"host": srv_cfg.host, "port": srv_cfg.port}},
    )

    log.info("moderation.listener.starting")
    listener.run_forever()


if __name__ == "__main__":
    main()
