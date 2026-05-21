"""notifier.py — sends a security notice email to the affected employee.

Uses the Gmail API v1 with service-account DWD to send email as
the configured sender address.  No user-ID lookup is needed: the
recipient is the chrome_email already captured during detection.

Required service-account scope (via DWD on sender_email):
  https://www.googleapis.com/auth/gmail.send
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

from googleapiclient.discovery import build  # type: ignore[import]
from googleapiclient.errors import HttpError  # type: ignore[import]
from google.oauth2 import service_account  # type: ignore[import]

from .config import BookmarkGuardConfig
from .errors import NotificationError
from .models import NotificationOutcome, RemovalOutcome, ScanResult

logger = logging.getLogger(__name__)

_GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
_EMAIL_SUBJECT = "Security Notice: Sensitive browser bookmarks detected and removed"


class GmailNotifier:
    def __init__(self, config: BookmarkGuardConfig) -> None:
        self._config = config

    # ------------------------------------------------------------------ public

    def notify(
        self,
        result: ScanResult,
        outcomes: list[RemovalOutcome],
    ) -> list[NotificationOutcome]:
        """Send one email per unique chrome_email with all of their violations."""
        if self._config.dry_run:
            logger.info("bookmark_guard.notifier.dry_run.skip")
            return []

        by_email: dict[str, list[RemovalOutcome]] = {}
        for outcome in outcomes:
            email = outcome.match.chrome_email
            if not email:
                logger.warning(
                    "bookmark_guard.notifier.no_email",
                    extra={"url": outcome.match.url, "profile": outcome.match.profile_dir},
                )
                continue
            by_email.setdefault(email, []).append(outcome)

        if not by_email:
            return []

        try:
            gmail_svc = _build_gmail_service(
                self._config.notification.service_account_key_path,
                self._config.notification.sender_email,
            )
        except Exception as exc:
            raise NotificationError(
                f"failed to initialise Gmail API client: {exc}"
            ) from exc

        results: list[NotificationOutcome] = []
        for recipient_email, email_outcomes in by_email.items():
            results.append(
                self._notify_user(gmail_svc, recipient_email, email_outcomes, result)
            )
        return results

    # -------------------------------------------------------------- per-user

    def _notify_user(
        self,
        gmail_svc,
        recipient_email: str,
        outcomes: list[RemovalOutcome],
        result: ScanResult,
    ) -> NotificationOutcome:
        sender = self._config.notification.sender_email
        body = _build_body(
            template=self._config.notification.message_template,
            recipient_email=recipient_email,
            hostname=result.hostname,
            outcomes=outcomes,
        )

        try:
            _send_email(gmail_svc, sender=sender, to=recipient_email,
                        subject=_EMAIL_SUBJECT, body=body)
            notified_at = datetime.now(timezone.utc)
            logger.info(
                "bookmark_guard.notifier.sent",
                extra={"to": recipient_email, "violations": len(outcomes)},
            )
            return NotificationOutcome(
                chrome_email=recipient_email, notified_at_utc=notified_at
            )
        except (HttpError, Exception) as exc:
            err = str(exc)
            logger.error(
                "bookmark_guard.notifier.send_failed",
                extra={"to": recipient_email, "error": err},
            )
            return NotificationOutcome(
                chrome_email=recipient_email, notified_at_utc=None, error=err
            )


# --------------------------------------------------------------- API helpers

def _build_gmail_service(key_path: Path, sender_email: str):
    creds = (
        service_account.Credentials.from_service_account_file(
            str(key_path), scopes=_GMAIL_SCOPES
        ).with_subject(sender_email)
    )
    return build("gmail", "v1", credentials=creds)


def _send_email(gmail_svc, *, sender: str, to: str, subject: str, body: str) -> None:
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail_svc.users().messages().send(userId="me", body={"raw": raw}).execute()


def _build_body(
    template: str,
    recipient_email: str,
    hostname: str,
    outcomes: list[RemovalOutcome],
) -> str:
    item_lines = []
    for o in outcomes:
        title = o.match.title or o.match.url
        status = "" if o.action_taken == "removed" else " [removal failed]"
        item_lines.append(f"  • {title}{status}\n    {o.match.url}")

    return template.format(
        display_name=recipient_email.split("@")[0].replace(".", " ").title(),
        hostname=hostname,
        count=len(outcomes),
        item_list="\n".join(item_lines),
    )
