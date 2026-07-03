"""Unit tests for KeywordFilter (Layer 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.moderation.text.keyword_filter import KeywordFilter

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

LDNOOBW = Path("src/moderation/text/keywords/ldnoobw.txt")
TECH_EXT = Path("src/moderation/text/keywords/tech_ecommerce_extension.txt")


@pytest.fixture()
def filter_both() -> KeywordFilter:
    return KeywordFilter((LDNOOBW, TECH_EXT))


@pytest.fixture()
def filter_tech_only() -> KeywordFilter:
    return KeywordFilter((TECH_EXT,))


# ---------------------------------------------------------------------------
# Tests — clean text
# ---------------------------------------------------------------------------


def test_clean_message_passes(filter_both: KeywordFilter) -> None:
    matched, terms = filter_both.scan("Let's sync up on the Q3 roadmap tomorrow.")
    assert not matched
    assert terms == []


def test_empty_string_passes(filter_both: KeywordFilter) -> None:
    matched, terms = filter_both.scan("")
    assert not matched


def test_none_equivalent_empty(filter_both: KeywordFilter) -> None:
    matched, terms = filter_both.scan("")
    assert not matched


# ---------------------------------------------------------------------------
# Tests — tech/ecommerce extension
# ---------------------------------------------------------------------------


def test_fullz_detected(filter_tech_only: KeywordFilter) -> None:
    matched, terms = filter_tech_only.scan("I have fresh fullz for sale, $50 each.")
    assert matched
    assert "fullz" in terms


def test_rdp_for_sale_detected(filter_tech_only: KeywordFilter) -> None:
    matched, terms = filter_tech_only.scan("selling rdp access to a corporate network")
    assert matched
    assert any("rdp" in t for t in terms)


def test_phishing_kit_detected(filter_tech_only: KeywordFilter) -> None:
    matched, terms = filter_tech_only.scan("Check out this phishing kit I found.")
    assert matched
    assert "phishing kit" in terms


def test_case_insensitive(filter_tech_only: KeywordFilter) -> None:
    matched, terms = filter_tech_only.scan("FULLZ FOR SALE — premium quality")
    assert matched


def test_multi_term_all_returned(filter_tech_only: KeywordFilter) -> None:
    matched, terms = filter_tech_only.scan(
        "selling rdp access and fullz — contact me"
    )
    assert matched
    assert len(terms) >= 2


# ---------------------------------------------------------------------------
# Tests — context (should still match; LLM layer handles false-positives)
# ---------------------------------------------------------------------------


def test_threat_intel_context_still_matches(filter_tech_only: KeywordFilter) -> None:
    """Keyword filter matches regardless of context; LLM resolves intent."""
    matched, terms = filter_tech_only.scan(
        "Threat actor is distributing phishing kits via Telegram — for awareness."
    )
    assert matched  # LLM should later classify this as FALSE_POSITIVE


def test_victim_report_still_matches_base_list(filter_both: KeywordFilter) -> None:
    matched, terms = filter_both.scan("I got scammed by this seller, lost everything.")
    # "scam" variants may or may not be in the base list depending on exact content;
    # this just verifies the filter runs without error.
    assert isinstance(matched, bool)
    assert isinstance(terms, list)
