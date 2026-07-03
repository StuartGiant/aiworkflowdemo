"""Unit tests for VisionAPIBackend — Google Cloud Vision API is mocked."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.moderation.config import ImageModerationConfig
from src.moderation.image.violence_detector import (
    LocalModelBackend,
    VisionAPIBackend,
    _score_to_action,
)
from src.moderation.models import ModerationAction


# ---------------------------------------------------------------------------
# Score-to-action mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "score,expected",
    [
        (0, ModerationAction.PASS),
        (25, ModerationAction.PASS),
        (50, ModerationAction.PASS),
        (51, ModerationAction.REVIEW),
        (60, ModerationAction.REVIEW),
        (70, ModerationAction.REVIEW),
        (71, ModerationAction.BLOCK),
        (80, ModerationAction.BLOCK),
        (95, ModerationAction.BLOCK),
        (100, ModerationAction.BLOCK),
    ],
)
def test_score_to_action(score: int, expected: ModerationAction) -> None:
    assert _score_to_action(score) == expected


# ---------------------------------------------------------------------------
# VisionAPIBackend (mocked Cloud Vision client)
# ---------------------------------------------------------------------------


def _make_backend(fallback: str = "review") -> VisionAPIBackend:
    config = ImageModerationConfig(fallback_on_api_error=fallback)
    with patch("src.moderation.image.violence_detector._vision_module"), \
         patch("src.moderation.image.violence_detector._sa_module"):
        backend = VisionAPIBackend(
            service_account_key_path=Path("fake/key.json"),
            config=config,
        )
    return backend


@pytest.mark.parametrize(
    "likelihood,expected_score,expected_action",
    [
        ("VERY_UNLIKELY", 10, ModerationAction.PASS),
        ("UNLIKELY", 30, ModerationAction.PASS),
        ("POSSIBLE", 60, ModerationAction.REVIEW),
        ("LIKELY", 80, ModerationAction.BLOCK),
        ("VERY_LIKELY", 95, ModerationAction.BLOCK),
    ],
)
def test_score_single_jpeg(
    likelihood: str, expected_score: int, expected_action: ModerationAction
) -> None:
    backend = _make_backend()

    mock_annotation = MagicMock()
    mock_annotation.violence.name = likelihood
    mock_response = MagicMock()
    mock_response.safe_search_annotation = mock_annotation
    mock_response.error.message = ""
    backend._client.safe_search_detection = MagicMock(return_value=mock_response)

    verdict = backend.score(b"\xff\xd8\xff", "image/jpeg")
    assert verdict.score == expected_score
    assert verdict.action == expected_action
    assert verdict.frames_scored == 1


def test_api_error_returns_review_fallback() -> None:
    backend = _make_backend(fallback="review")
    backend._client.safe_search_detection = MagicMock(side_effect=RuntimeError("API down"))

    verdict = backend.score(b"\xff\xd8\xff", "image/jpeg")
    assert verdict.score is None
    assert verdict.action == ModerationAction.REVIEW


def test_api_error_returns_block_fallback_when_configured() -> None:
    backend = _make_backend(fallback="block")
    backend._client.safe_search_detection = MagicMock(side_effect=RuntimeError("API down"))

    verdict = backend.score(b"\xff\xd8\xff", "image/jpeg")
    assert verdict.score is None
    assert verdict.action == ModerationAction.BLOCK


# ---------------------------------------------------------------------------
# LocalModelBackend — Phase 2 stub
# ---------------------------------------------------------------------------


def test_local_model_backend_not_implemented() -> None:
    config = ImageModerationConfig(backend="local_model")
    with pytest.raises(NotImplementedError, match="Phase 2"):
        LocalModelBackend(config=config)
