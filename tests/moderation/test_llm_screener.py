"""Unit tests for LLMScreener (Layer 2) — Anthropic API is mocked."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.moderation.config import LLMConfig
from src.moderation.text.llm_screener import (
    LLMScreener,
    LLMScreenerUnavailableError,
    ScreenerResult,
    _parse_response,
)


# ---------------------------------------------------------------------------
# _parse_response unit tests
# ---------------------------------------------------------------------------


def test_parse_true_positive() -> None:
    raw = '{"verdict": "TRUE_POSITIVE", "rationale": "Active fraud offer."}'
    result = _parse_response(raw)
    assert result.verdict == "TRUE_POSITIVE"
    assert result.rationale == "Active fraud offer."


def test_parse_false_positive() -> None:
    raw = '{"verdict": "FALSE_POSITIVE", "rationale": "Educational context."}'
    result = _parse_response(raw)
    assert result.verdict == "FALSE_POSITIVE"


def test_parse_malformed_defaults_to_true_positive() -> None:
    """Malformed LLM output must default conservatively to TRUE_POSITIVE."""
    result = _parse_response("not json at all")
    assert result.verdict == "TRUE_POSITIVE"
    assert "parse error" in result.rationale


def test_parse_unknown_verdict_defaults_to_true_positive() -> None:
    result = _parse_response('{"verdict": "MAYBE", "rationale": "unsure"}')
    assert result.verdict == "TRUE_POSITIVE"


# ---------------------------------------------------------------------------
# LLMScreener integration (mocked client)
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_config() -> LLMConfig:
    return LLMConfig(model="claude-sonnet-4-6", max_tokens=256, timeout_seconds=10)


def _make_screener(config: LLMConfig) -> LLMScreener:
    """Build an LLMScreener with the Anthropic client constructor mocked."""
    with patch("src.moderation.text.llm_screener.anthropic.Anthropic") as MockClient:
        screener = LLMScreener(api_key="test-key", config=config)
        # Replace the real client instance with a fresh MagicMock
        screener._client = MockClient.return_value
    return screener


def test_screen_true_positive(mock_config: LLMConfig) -> None:
    screener = _make_screener(mock_config)

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text='{"verdict":"TRUE_POSITIVE","rationale":"Active fraud."}')]
    screener._client.messages.create = MagicMock(return_value=mock_response)

    result = screener.screen("buy fullz here", ["fullz"])
    assert result.verdict == "TRUE_POSITIVE"


def test_screen_false_positive(mock_config: LLMConfig) -> None:
    screener = _make_screener(mock_config)

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text='{"verdict":"FALSE_POSITIVE","rationale":"Threat intel."}')]
    screener._client.messages.create = MagicMock(return_value=mock_response)

    result = screener.screen("threat actor uses phishing kits", ["phishing kit"])
    assert result.verdict == "FALSE_POSITIVE"


def test_screen_raises_on_connection_error(mock_config: LLMConfig) -> None:
    import anthropic

    screener = _make_screener(mock_config)
    screener._client.messages.create = MagicMock(
        side_effect=anthropic.APIConnectionError(request=MagicMock())
    )

    with pytest.raises(LLMScreenerUnavailableError):
        screener.screen("some text", ["term"])


def test_screen_raises_on_timeout(mock_config: LLMConfig) -> None:
    import anthropic

    screener = _make_screener(mock_config)
    screener._client.messages.create = MagicMock(
        side_effect=anthropic.APITimeoutError(request=MagicMock())
    )

    with pytest.raises(LLMScreenerUnavailableError):
        screener.screen("some text", ["term"])


def test_no_api_key_raises() -> None:
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        LLMScreener(api_key="", config=LLMConfig())
