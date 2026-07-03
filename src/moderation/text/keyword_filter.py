"""Layer 1 text moderation — keyword filter.

Loads keyword lists in two tiers:
  - hard_block: terms that auto-block without LLM review
  - soft_flag:  terms that are sent to the LLM screener for context

Returns which tier matched so the caller can route accordingly.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

_WORD_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _word_pattern(term: str) -> re.Pattern[str]:
    if term not in _WORD_RE_CACHE:
        _WORD_RE_CACHE[term] = re.compile(
            r"\b" + re.escape(term) + r"\b", re.IGNORECASE
        )
    return _WORD_RE_CACHE[term]


@dataclass(frozen=True)
class KeywordScanResult:
    hard_block_terms: list[str] = field(default_factory=list)
    soft_flag_terms: list[str] = field(default_factory=list)

    @property
    def has_hard_block(self) -> bool:
        return bool(self.hard_block_terms)

    @property
    def has_soft_flag(self) -> bool:
        return bool(self.soft_flag_terms)

    @property
    def all_terms(self) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for t in self.hard_block_terms + self.soft_flag_terms:
            if t not in seen:
                seen.add(t)
                result.append(t)
        return result


class _TermIndex:
    """Holds words and phrases from a single keyword list file set."""

    def __init__(self) -> None:
        self.words: list[str] = []
        self.phrases: list[str] = []

    def load(self, path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(f"Keyword list not found: {path}")
        with path.open(encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                term = line.lower()
                if " " in term:
                    self.phrases.append(term)
                else:
                    self.words.append(term)

    @property
    def total(self) -> int:
        return len(self.words) + len(self.phrases)

    def scan(self, lower_text: str) -> list[str]:
        found: list[str] = []
        for term in self.words:
            if _word_pattern(term).search(lower_text):
                found.append(term)
        for phrase in self.phrases:
            if phrase in lower_text:
                found.append(phrase)
        return found


class KeywordFilter:
    """Two-tier keyword matcher: hard_block and soft_flag lists.

    hard_block matches auto-block without LLM review.
    soft_flag matches are forwarded to the LLM screener for context.
    """

    def __init__(
        self,
        hard_block_paths: tuple[Path, ...],
        soft_flag_paths: tuple[Path, ...],
    ) -> None:
        self._hard = _TermIndex()
        self._soft = _TermIndex()

        for path in hard_block_paths:
            self._hard.load(path)
        for path in soft_flag_paths:
            self._soft.load(path)

        log.info(
            "moderation.keyword_filter.loaded",
            extra={
                "context": {
                    "hard_block_terms": self._hard.total,
                    "soft_flag_terms": self._soft.total,
                    "hard_block_files": [str(p) for p in hard_block_paths],
                    "soft_flag_files": [str(p) for p in soft_flag_paths],
                }
            },
        )

    def scan(self, text: str) -> KeywordScanResult:
        """Scan text and return which tier(s) matched."""
        if not text:
            return KeywordScanResult()

        lower_text = text.lower()

        # Hard block checked first — phrases before single words to catch
        # the more specific match (e.g. "fullz for sale" before "fullz")
        hard_found = self._hard.scan(lower_text)
        soft_found = self._soft.scan(lower_text)

        return KeywordScanResult(
            hard_block_terms=hard_found,
            soft_flag_terms=soft_found,
        )
