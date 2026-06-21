"""chat_responder — replaces flagged messages with a tombstone and DMs the reviewer.

Uses the Google Chat REST API via the existing service account credentials.
"""

from __future__ import annotations

import logging
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ..models import ContentItem, ModerationAction, ModerationDecision
from .card_builder import build_alert_card

log = logging.getLogger(__name__)

_DWD_SCOPES = [
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
]

_BOT_SCOPES = [
    "https://www.googleapis.com/auth/chat.bot",
]


class ChatResponder:
    """Replaces blocked messages with a tombstone and sends card DMs to the reviewer."""

    def __init__(
        self,
        service_account_key_path: Path,
        admin_email: str,
        reviewer_chat_user_id: str,
    ) -> None:
        # DWD credentials for message patch (needs space membership context)
        dwd_creds = (
            service_account.Credentials.from_service_account_file(
                str(service_account_key_path),
                scopes=_DWD_SCOPES,
            ).with_subject(admin_email)
        )
        self._svc = build("chat", "v1", credentials=dwd_creds, cache_discovery=False)

        # Bot credentials (no DWD) for opening DMs and sending card notifications
        bot_creds = service_account.Credentials.from_service_account_file(
            str(service_account_key_path),
            scopes=_BOT_SCOPES,
        )
        self._bot_svc = build("chat", "v1", credentials=bot_creds, cache_discovery=False)

        self._reviewer_user_id = reviewer_chat_user_id

    def handle(self, decision: ModerationDecision, case_id: str | None) -> None:
        """Replace the message if BLOCK, then notify reviewer if REVIEW or BLOCK."""
        if decision.final_action == ModerationAction.PASS:
            return

        if decision.final_action == ModerationAction.BLOCK:
            self._delete_message(decision.content)

        if self._reviewer_user_id:
            self._notify_reviewer(decision, case_id)
        else:
            log.warning(
                "moderation.chat_responder.no_reviewer_id",
                extra={"context": {"message": decision.content.message_name}},
            )

    # ------------------------------------------------------------------

    def _find_or_create_dm(self) -> str | None:
        """Return the DM space name between the bot and the reviewer.

        Tries findDirectMessage first; creates the space if it doesn't exist yet.
        """
        try:
            dm = self._bot_svc.spaces().findDirectMessage(
                name=self._reviewer_user_id
            ).execute()
            return dm["name"]
        except HttpError as exc:
            if exc.resp.status != 404:
                log.error(
                    "moderation.chat_responder.dm_space_error",
                    extra={"context": {"reviewer": self._reviewer_user_id, "err": str(exc)}},
                )
                return None

        try:
            dm = self._bot_svc.spaces().setup(
                body={
                    "space": {"spaceType": "DIRECT_MESSAGE"},
                    "memberships": [
                        {"member": {"name": self._reviewer_user_id, "type": "HUMAN"}}
                    ],
                }
            ).execute()
            return dm["name"]
        except HttpError as exc:
            log.error(
                "moderation.chat_responder.dm_create_error",
                extra={"context": {"reviewer": self._reviewer_user_id, "err": str(exc)}},
            )
            return None

    _TOMBSTONE = "🚫 This content has been removed by the Security Content Moderation system."

    def _delete_message(self, content: ContentItem) -> None:
        try:
            self._svc.spaces().messages().patch(
                name=content.message_name,
                updateMask="text,attachment",
                body={"text": self._TOMBSTONE, "attachment": []},
            ).execute()
            log.info(
                "moderation.chat_responder.message_replaced",
                extra={"context": {"message": content.message_name}},
            )
        except HttpError as exc:
            log.error(
                "moderation.chat_responder.replace_failed",
                extra={
                    "context": {
                        "message": content.message_name,
                        "status": exc.resp.status,
                        "err": str(exc),
                    }
                },
            )

    def _notify_reviewer(
        self, decision: ModerationDecision, case_id: str | None
    ) -> None:
        action_label = decision.final_action.value.upper()
        card = build_alert_card(decision, case_id, action_label)

        space_name = self._find_or_create_dm()
        if not space_name:
            return

        try:
            self._bot_svc.spaces().messages().create(
                parent=space_name,
                body={"cardsV2": [card]},
            ).execute()
            log.info(
                "moderation.chat_responder.reviewer_notified",
                extra={"context": {"action": action_label, "case_id": case_id}},
            )
        except HttpError as exc:
            log.error(
                "moderation.chat_responder.notify_failed",
                extra={"context": {"err": str(exc)}},
            )
