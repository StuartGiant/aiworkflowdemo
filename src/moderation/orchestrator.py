"""Content moderation orchestrator.

Combines TextModerator and ImageModerator verdicts, applies the combined
action rules, and triggers the appropriate actions (delete, notify, record).

Action rules:
  - Either layer BLOCK  → final action = BLOCK
  - Either layer REVIEW (and none BLOCK) → final action = REVIEW
  - Both layers PASS   → final action = PASS
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Optional

from .actions.case_writer import CaseWriter
from .actions.chat_responder import ChatResponder
from .actions.email_notifier import EmailNotifier
from .config import ModerationConfig
from .image.moderator import ImageModerator
from .models import (
    ContentItem,
    ImageVerdict,
    ModerationAction,
    ModerationDecision,
    TextVerdict,
)
from .text.moderator import TextModerator

log = logging.getLogger(__name__)

_ENGINE_VERSION = "1.0.0"


class ModerationOrchestrator:
    """Top-level coordinator for the content moderation pipeline."""

    def __init__(self, config: ModerationConfig, db_dsn: str, admin_email: str, dry_run: bool = False) -> None:
        self._config = config
        self._dry_run = dry_run
        self._text_mod = TextModerator(
            config=config.text,
            anthropic_api_key=config.anthropic_api_key,
        )
        self._image_mod = ImageModerator(
            config=config.image,
            service_account_key_path=config.service_account_key_path,
        )
        self._case_writer = CaseWriter(dsn=db_dsn)
        self._chat_responder = ChatResponder(
            service_account_key_path=config.service_account_key_path,
            admin_email=admin_email,
            reviewer_chat_user_id=config.actions.reviewer_chat_user_id,
        )
        self._email_notifier = EmailNotifier(
            service_account_key_path=config.service_account_key_path,
            sender_email=admin_email,
            reviewer_email=config.actions.reviewer_email,
        )

    def moderate(self, content: ContentItem) -> ModerationDecision:
        """Run a ContentItem through all moderation layers and execute actions.

        Args:
            content: The message to moderate.

        Returns:
            ModerationDecision capturing the full audit trail.
        """
        log.info(
            "moderation.orchestrator.start",
            extra={
                "context": {
                    "message": content.message_name,
                    "sender": content.sender_email,
                    "has_text": bool(content.text),
                    "image_count": len(content.images),
                }
            },
        )

        # --- Text layer ---
        text_verdict: TextVerdict = self._text_mod.moderate(content.text)

        # --- Image layer ---
        image_verdicts: list[ImageVerdict] = [
            self._image_mod.moderate(img) for img in content.images
        ]

        # --- Combine ---
        final_action = _combine_actions(text_verdict, image_verdicts)

        decision = ModerationDecision(
            content=content,
            text_verdict=text_verdict,
            image_verdicts=tuple(image_verdicts),
            final_action=final_action,
            engine_version=_ENGINE_VERSION,
            llm_model=self._config.text.llm.model if text_verdict.llm_rationale is not None else None,
            vision_backend=self._config.image.backend,
        )

        log.info(
            "moderation.orchestrator.verdict",
            extra={
                "context": {
                    "message": content.message_name,
                    "text_result": text_verdict.result.value,
                    "image_actions": [v.action.value for v in image_verdicts],
                    "final_action": final_action.value,
                }
            },
        )

        # --- Actions ---
        if final_action != ModerationAction.PASS:
            if self._dry_run:
                log.info(
                    "moderation.orchestrator.dry_run",
                    extra={
                        "context": {
                            "message": content.message_name,
                            "final_action": final_action.value,
                            "skipped": ["case_write", "chat_respond", "email_notify"],
                        }
                    },
                )
            else:
                case_id_uuid: Optional[uuid.UUID] = self._case_writer.record(decision)
                case_id_str = str(case_id_uuid) if case_id_uuid else None

                self._chat_responder.handle(decision, case_id=case_id_str)
                self._email_notifier.notify(decision, case_id=case_id_str)

        return decision


def _combine_actions(
    text_verdict: TextVerdict,
    image_verdicts: list[ImageVerdict],
) -> ModerationAction:
    """Apply combination rules to derive the final action."""
    all_actions = [text_verdict.action] + [v.action for v in image_verdicts]

    if ModerationAction.BLOCK in all_actions:
        return ModerationAction.BLOCK
    if ModerationAction.REVIEW in all_actions:
        return ModerationAction.REVIEW
    return ModerationAction.PASS
