"""Text moderation orchestrator — combines keyword tiers and LLM screener.

Decision flow:
  1. Keyword filter scans the text across two tiers.
  2. No match on either tier → PASS (LLM not called).
  3. Hard-block match → BLOCK immediately (LLM not called).
  4. Soft-flag match only → LLM screener determines TRUE_POSITIVE / FALSE_POSITIVE.
  5. LLM unavailable on soft-flag match → FLAGGED_FALLBACK (fail-safe).
"""

from __future__ import annotations

import logging
from typing import Optional

from ..config import TextModerationConfig
from ..models import TextVerdict, TextVerdictResult
from .keyword_filter import KeywordFilter
from .llm_screener import LLMScreener, LLMScreenerUnavailableError

log = logging.getLogger(__name__)


class TextModerator:
    """Two-layer text moderation pipeline with tiered keyword routing."""

    def __init__(
        self,
        config: TextModerationConfig,
        anthropic_api_key: str,
    ) -> None:
        self._filter = KeywordFilter(
            hard_block_paths=config.hard_block_keyword_list_paths,
            soft_flag_paths=config.soft_flag_keyword_list_paths,
        )
        self._screener: Optional[LLMScreener] = None
        if anthropic_api_key:
            self._screener = LLMScreener(api_key=anthropic_api_key, config=config.llm)
        else:
            log.warning(
                "moderation.text_moderator.no_llm",
                extra={"context": {"reason": "ANTHROPIC_API_KEY not set; Layer 2 disabled"}},
            )

    def moderate(self, text: Optional[str]) -> TextVerdict:
        if not text or not text.strip():
            return TextVerdict(result=TextVerdictResult.PASS)

        scan = self._filter.scan(text)

        # --- No match: pass immediately ---
        if not scan.has_hard_block and not scan.has_soft_flag:
            log.debug("moderation.text.pass", extra={"context": {"layer": 1}})
            return TextVerdict(result=TextVerdictResult.PASS)

        # --- Hard block: auto-block, no LLM ---
        if scan.has_hard_block:
            log.info(
                "moderation.text.hard_block",
                extra={
                    "context": {
                        "terms": scan.hard_block_terms,
                        "count": len(scan.hard_block_terms),
                    }
                },
            )
            return TextVerdict(
                result=TextVerdictResult.TRUE_POSITIVE,
                matched_terms=tuple(scan.all_terms),
            )

        # --- Soft flag: send to LLM for context ---
        log.info(
            "moderation.text.keyword_match",
            extra={
                "context": {
                    "terms": scan.soft_flag_terms,
                    "count": len(scan.soft_flag_terms),
                    "tier": "soft_flag",
                }
            },
        )

        if self._screener is None:
            log.info(
                "moderation.text.flagged_fallback",
                extra={"context": {"reason": "LLM screener not configured"}},
            )
            return TextVerdict(
                result=TextVerdictResult.FLAGGED_FALLBACK,
                matched_terms=tuple(scan.soft_flag_terms),
            )

        try:
            result = self._screener.screen(text, scan.soft_flag_terms)
        except LLMScreenerUnavailableError as exc:
            log.warning(
                "moderation.text.llm_unavailable",
                extra={"context": {"err": str(exc)}},
            )
            return TextVerdict(
                result=TextVerdictResult.FLAGGED_FALLBACK,
                matched_terms=tuple(scan.soft_flag_terms),
                llm_rationale=f"LLM unavailable: {exc}",
            )

        verdict_result = (
            TextVerdictResult.TRUE_POSITIVE
            if result.verdict == "TRUE_POSITIVE"
            else TextVerdictResult.FALSE_POSITIVE
        )

        log.info(
            "moderation.text.llm_verdict",
            extra={
                "context": {
                    "verdict": verdict_result.value,
                    "rationale": result.rationale,
                }
            },
        )

        return TextVerdict(
            result=verdict_result,
            matched_terms=tuple(scan.soft_flag_terms),
            llm_rationale=result.rationale,
        )
