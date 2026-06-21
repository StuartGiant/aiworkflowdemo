"""email_notifier — sends review/block notifications via Gmail API.

Uses the existing service account DWD credentials to send on behalf of
the admin email address.
"""

from __future__ import annotations

import base64
import email.mime.text
import logging
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ..models import ModerationAction, ModerationDecision

log = logging.getLogger(__name__)

_GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


class EmailNotifier:
    """Sends moderation alert emails via the Gmail API."""

    def __init__(
        self,
        service_account_key_path: Path,
        sender_email: str,
        reviewer_email: str,
    ) -> None:
        credentials = (
            service_account.Credentials.from_service_account_file(
                str(service_account_key_path),
                scopes=_GMAIL_SCOPES,
            ).with_subject(sender_email)
        )
        self._svc = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        self._sender = sender_email
        self._reviewer = reviewer_email

    def notify(self, decision: ModerationDecision, case_id: str | None) -> None:
        """Send a review notification email for REVIEW or BLOCK decisions."""
        if decision.final_action == ModerationAction.PASS:
            return
        if not self._reviewer:
            log.warning("moderation.email_notifier.no_reviewer_email")
            return

        action = decision.final_action.value.upper()
        content = decision.content

        subject = f"[Content Moderation — {action}] {content.space_name}"
        body = self._build_body(decision, case_id)

        msg = email.mime.text.MIMEText(body, "plain")
        msg["to"] = self._reviewer
        msg["from"] = self._sender
        msg["subject"] = subject

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        try:
            self._svc.users().messages().send(
                userId="me",
                body={"raw": raw},
            ).execute()
            log.info(
                "moderation.email_notifier.sent",
                extra={"context": {"to": self._reviewer, "action": action, "case_id": case_id}},
            )
        except HttpError as exc:
            log.error(
                "moderation.email_notifier.send_failed",
                extra={"context": {"err": str(exc), "status": exc.resp.status}},
            )

    @staticmethod
    def _build_body(decision: ModerationDecision, case_id: str | None) -> str:
        action = decision.final_action.value.upper()
        c = decision.content
        lines = [
            f"Content Moderation Alert — {action}",
            "=" * 50,
            "",
            f"Sender:    {c.sender_email}",
            f"Space:     {c.space_name}",
            f"Message:   {c.message_name}",
            f"Time (UTC):{c.received_at_utc.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Case ID:   {case_id or 'N/A'}",
            "",
            "--- Text Moderation ---",
            f"Result:    {decision.text_verdict.result.value}",
        ]

        if decision.text_verdict.matched_terms:
            lines.append(f"Terms:     {', '.join(decision.text_verdict.matched_terms)}")
        if decision.text_verdict.llm_rationale:
            lines.append(f"Rationale: {decision.text_verdict.llm_rationale}")

        worst_img = decision.worst_image_verdict
        if worst_img:
            lines += [
                "",
                "--- Image Moderation ---",
                f"Score:     {worst_img.score if worst_img.score is not None else 'N/A (API error)'}",
                f"Action:    {worst_img.action.value}",
                f"Format:    {worst_img.image_format.value if worst_img.image_format else 'unknown'}",
                f"Frames:    {worst_img.frames_scored}",
            ]

        lines += [
            "",
            "--- Required Action ---",
            "Please review the flagged content and update the case disposition",
            "(true_positive / false_positive / inconclusive) in the case database.",
            "",
            f"Engine version: {decision.engine_version}",
        ]

        return "\n".join(lines)
