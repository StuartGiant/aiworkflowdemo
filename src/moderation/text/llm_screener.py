"""Layer 2 text moderation — LLM semantic screener.

Called only when Layer 1 (keyword filter) returns a match. Uses the Anthropic
API to determine whether the flagged content is a genuine policy violation
(TRUE_POSITIVE) or a benign false alarm (FALSE_POSITIVE).

Degrades gracefully: if the API is unreachable or times out, the caller
receives LLMScreenerUnavailableError and should fall back to the keyword result.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

import anthropic

from ..config import LLMConfig

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a content moderation assistant for a technology company's
internal communication platform (Google Chat). Your task is to determine whether a
flagged message constitutes a genuine policy violation.

Policy violations include:
- Active fraud, scam, or phishing solicitations
- Offers to sell stolen credentials, payment card data, or company data
- Distribution of malware, exploit kits, or hacking tools
- Threats of violence or harassment
- Explicit sexual content
- Hate speech or discriminatory language used as an attack

NOT violations (false positives):
- Discussing security topics in an educational or defensive context
- Reporting that one has been a victim ("I got scammed")
- Using profanity casually without targeting anyone
- Security researchers referencing threat actor TTPs
- News articles or threat intelligence shared for awareness

Respond ONLY with a JSON object in this exact format:
{"verdict": "TRUE_POSITIVE" | "FALSE_POSITIVE", "rationale": "<one sentence explanation>"}"""


class ScreenerResult(NamedTuple):
    verdict: str       # "TRUE_POSITIVE" | "FALSE_POSITIVE"
    rationale: str


class LLMScreenerUnavailableError(Exception):
    """Raised when the Anthropic API cannot be reached or times out."""


class LLMScreener:
    """Semantic content screener backed by the Anthropic Claude API."""

    def __init__(self, api_key: str, config: LLMConfig) -> None:
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is required for LLMScreener")
        self._client = anthropic.Anthropic(api_key=api_key)
        self._config = config

    def screen(self, text: str, matched_terms: list[str]) -> ScreenerResult:
        """Determine whether *text* is a genuine policy violation.

        Args:
            text: The original message text.
            matched_terms: Terms that triggered the keyword filter.

        Returns:
            ScreenerResult with verdict and rationale.

        Raises:
            LLMScreenerUnavailableError: if the API call fails.
        """
        user_message = (
            f"Flagged terms: {', '.join(matched_terms)}\n\n"
            f"Message content:\n{text}"
        )

        try:
            response = self._client.messages.create(
                model=self._config.model,
                max_tokens=self._config.max_tokens,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
                timeout=self._config.timeout_seconds,
            )
        except anthropic.APIConnectionError as exc:
            log.warning(
                "moderation.llm_screener.unavailable",
                extra={"context": {"err": str(exc)}},
            )
            raise LLMScreenerUnavailableError(f"API connection error: {exc}") from exc
        except anthropic.APITimeoutError as exc:
            log.warning(
                "moderation.llm_screener.timeout",
                extra={"context": {"timeout_s": self._config.timeout_seconds}},
            )
            raise LLMScreenerUnavailableError(f"API timeout after {self._config.timeout_seconds}s") from exc
        except anthropic.APIStatusError as exc:
            log.warning(
                "moderation.llm_screener.api_error",
                extra={"context": {"status": exc.status_code, "err": str(exc)}},
            )
            raise LLMScreenerUnavailableError(f"API error {exc.status_code}: {exc}") from exc

        raw = response.content[0].text.strip()
        return _parse_response(raw)


def _parse_response(raw: str) -> ScreenerResult:
    """Parse the JSON response from the LLM, with fallback on malformed output."""
    import json

    try:
        data = json.loads(raw)
        verdict = data.get("verdict", "").upper()
        if verdict not in ("TRUE_POSITIVE", "FALSE_POSITIVE"):
            raise ValueError(f"unexpected verdict: {verdict!r}")
        return ScreenerResult(verdict=verdict, rationale=data.get("rationale", ""))
    except (json.JSONDecodeError, ValueError, KeyError) as exc:
        log.warning(
            "moderation.llm_screener.parse_error",
            extra={"context": {"raw": raw[:200], "err": str(exc)}},
        )
        # If we can't parse the response, treat conservatively as TRUE_POSITIVE
        return ScreenerResult(
            verdict="TRUE_POSITIVE",
            rationale=f"[parse error — defaulting to TRUE_POSITIVE] raw={raw[:100]}",
        )
