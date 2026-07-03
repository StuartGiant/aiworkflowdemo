"""Integration tests for ModerationOrchestrator — all external calls mocked."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.moderation.models import (
    ContentItem,
    ImageAttachment,
    ImageFormat,
    ImageVerdict,
    ModerationAction,
    TextVerdict,
    TextVerdictResult,
)
from src.moderation.orchestrator import ModerationOrchestrator, _combine_actions


# ---------------------------------------------------------------------------
# _combine_actions unit tests
# ---------------------------------------------------------------------------


def _tv(action: ModerationAction) -> TextVerdict:
    """Build a TextVerdict whose .action property returns the given action.

    Text verdicts can only produce PASS or BLOCK natively. For REVIEW we
    return a PASS verdict and rely solely on the image layer to supply REVIEW
    (which is what _combine_actions tests for).  The parametrised test case
    (REVIEW, [], REVIEW) is therefore removed — text alone cannot be REVIEW.
    """
    result_map = {
        ModerationAction.PASS: TextVerdictResult.PASS,
        ModerationAction.BLOCK: TextVerdictResult.TRUE_POSITIVE,
    }
    return TextVerdict(result=result_map[action])


def _iv(action: ModerationAction) -> ImageVerdict:
    score_map = {
        ModerationAction.PASS: 10,
        ModerationAction.REVIEW: 60,
        ModerationAction.BLOCK: 80,
    }
    return ImageVerdict(score=score_map[action], action=action)


@pytest.mark.parametrize(
    "text_action,image_actions,expected",
    [
        # Text=PASS cases
        (ModerationAction.PASS, [], ModerationAction.PASS),
        (ModerationAction.PASS, [ModerationAction.PASS], ModerationAction.PASS),
        (ModerationAction.PASS, [ModerationAction.REVIEW], ModerationAction.REVIEW),
        (ModerationAction.PASS, [ModerationAction.BLOCK], ModerationAction.BLOCK),
        # Text=BLOCK cases (text layer never produces REVIEW)
        (ModerationAction.BLOCK, [], ModerationAction.BLOCK),
        (ModerationAction.BLOCK, [ModerationAction.REVIEW], ModerationAction.BLOCK),
        (ModerationAction.BLOCK, [ModerationAction.PASS], ModerationAction.BLOCK),
    ],
)
def test_combine_actions(
    text_action: ModerationAction,
    image_actions: list[ModerationAction],
    expected: ModerationAction,
) -> None:
    text_verdict = _tv(text_action)
    image_verdicts = [_iv(a) for a in image_actions]
    assert _combine_actions(text_verdict, image_verdicts) == expected


# ---------------------------------------------------------------------------
# Orchestrator end-to-end (all external I/O mocked)
# ---------------------------------------------------------------------------


def _make_content(text: str = "", images: list[ImageAttachment] | None = None) -> ContentItem:
    return ContentItem(
        message_name="spaces/abc/messages/123",
        space_name="spaces/abc",
        sender_email="user@corp.com",
        text=text,
        images=tuple(images or []),
        received_at_utc=datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc),
    )


@pytest.fixture()
def orchestrator() -> ModerationOrchestrator:
    """Build an orchestrator with all external services mocked."""
    from src.moderation.config import (
        ActionsConfig,
        ImageModerationConfig,
        LLMConfig,
        ModerationConfig,
        PubSubConfig,
        TextModerationConfig,
    )

    config = ModerationConfig(
        text=TextModerationConfig(
            keyword_list_paths=(
                Path("src/moderation/text/keywords/ldnoobw.txt"),
                Path("src/moderation/text/keywords/tech_ecommerce_extension.txt"),
            ),
            llm=LLMConfig(),
        ),
        image=ImageModerationConfig(),
        actions=ActionsConfig(
            reviewer_email="reviewer@corp.com",
            reviewer_chat_user_id="users/reviewer123",
        ),
        pubsub=PubSubConfig(),
        _anthropic_api_key="test-key",
        _service_account_key_path="fake/key.json",
    )

    with patch("src.moderation.image.violence_detector._vision_module"), \
         patch("src.moderation.image.violence_detector._sa_module"), \
         patch("src.moderation.actions.case_writer.psycopg"), \
         patch("src.moderation.actions.chat_responder.build"), \
         patch("src.moderation.actions.chat_responder.service_account"), \
         patch("src.moderation.actions.email_notifier.build"), \
         patch("src.moderation.actions.email_notifier.service_account"), \
         patch("src.moderation.text.llm_screener.anthropic.Anthropic"):
        orch = ModerationOrchestrator(
            config=config,
            db_dsn="postgresql://fake/fake",
            admin_email="admin@corp.com",
        )

    return orch


def test_clean_message_passes(orchestrator: ModerationOrchestrator) -> None:
    content = _make_content(text="Good morning team, standup in 10 minutes!")

    with patch.object(orchestrator._case_writer, "record") as mock_record, \
         patch.object(orchestrator._chat_responder, "handle") as mock_chat, \
         patch.object(orchestrator._email_notifier, "notify") as mock_email:

        decision = orchestrator.moderate(content)

    assert decision.final_action == ModerationAction.PASS
    mock_record.assert_not_called()
    mock_chat.assert_not_called()
    mock_email.assert_not_called()


def test_flagged_text_triggers_block(orchestrator: ModerationOrchestrator) -> None:
    content = _make_content(text="Fresh fullz for sale — $50 each DM me")

    mock_screener_result = MagicMock()
    mock_screener_result.verdict = "TRUE_POSITIVE"
    mock_screener_result.rationale = "Active fraud offer."

    with patch.object(orchestrator._text_mod._screener, "screen", return_value=mock_screener_result), \
         patch.object(orchestrator._case_writer, "record", return_value=None) as mock_record, \
         patch.object(orchestrator._chat_responder, "handle") as mock_chat, \
         patch.object(orchestrator._email_notifier, "notify") as mock_email:

        decision = orchestrator.moderate(content)

    assert decision.final_action == ModerationAction.BLOCK
    mock_record.assert_called_once()
    mock_chat.assert_called_once()
    mock_email.assert_called_once()


def test_image_review_triggers_notification(orchestrator: ModerationOrchestrator) -> None:
    img = ImageAttachment(data=b"\xff\xd8\xff", mime_type="image/jpeg")
    content = _make_content(images=[img])

    review_verdict = ImageVerdict(score=60, action=ModerationAction.REVIEW, image_format=ImageFormat.JPEG)

    with patch.object(orchestrator._image_mod, "moderate", return_value=review_verdict), \
         patch.object(orchestrator._case_writer, "record", return_value=None) as mock_record, \
         patch.object(orchestrator._chat_responder, "handle") as mock_chat, \
         patch.object(orchestrator._email_notifier, "notify") as mock_email:

        decision = orchestrator.moderate(content)

    assert decision.final_action == ModerationAction.REVIEW
    mock_record.assert_called_once()
    mock_chat.assert_called_once()
    mock_email.assert_called_once()


def test_dry_run_skips_all_actions(orchestrator: ModerationOrchestrator) -> None:
    content = _make_content(text="Fresh fullz for sale — $50 each DM me")

    mock_screener_result = MagicMock()
    mock_screener_result.verdict = "TRUE_POSITIVE"
    mock_screener_result.rationale = "Active fraud offer."

    orchestrator._dry_run = True

    with patch.object(orchestrator._text_mod._screener, "screen", return_value=mock_screener_result), \
         patch.object(orchestrator._case_writer, "record") as mock_record, \
         patch.object(orchestrator._chat_responder, "handle") as mock_chat, \
         patch.object(orchestrator._email_notifier, "notify") as mock_email:

        decision = orchestrator.moderate(content)

    assert decision.final_action == ModerationAction.BLOCK
    mock_record.assert_not_called()
    mock_chat.assert_not_called()
    mock_email.assert_not_called()


def test_llm_unavailable_falls_back_to_keyword_result(
    orchestrator: ModerationOrchestrator,
) -> None:
    from src.moderation.text.llm_screener import LLMScreenerUnavailableError

    content = _make_content(text="selling rdp access to corporate network")

    with patch.object(
        orchestrator._text_mod._screener,
        "screen",
        side_effect=LLMScreenerUnavailableError("timeout"),
    ), \
         patch.object(orchestrator._case_writer, "record", return_value=None), \
         patch.object(orchestrator._chat_responder, "handle"), \
         patch.object(orchestrator._email_notifier, "notify"):

        decision = orchestrator.moderate(content)

    assert decision.text_verdict.result == TextVerdictResult.FLAGGED_FALLBACK
    assert decision.final_action == ModerationAction.BLOCK
