"""Builds Google Chat cardsV2 for moderation alert messages."""

from __future__ import annotations

import hashlib
import hmac as _hmac

from ..models import ModerationAction, ModerationDecision

_DISPOSITION_LABELS = {
    "true_positive": "✅ TRUE POSITIVE",
    "false_positive": "❌ FALSE POSITIVE",
    "inconclusive": "❓ INCONCLUSIVE",
}

# Set by the main script at startup — used to build button URLs.
_interaction_base_url: str = ""
_hmac_secret: bytes = b""


def set_interaction_base_url(url: str) -> None:
    global _interaction_base_url
    _interaction_base_url = url.rstrip("/")


def set_hmac_secret(secret: str) -> None:
    global _hmac_secret
    _hmac_secret = secret.encode()


def _sign_disposition(case_id: str, disposition: str) -> str:
    """Return hex HMAC-SHA256 of 'case_id:disposition' using the shared secret."""
    msg = f"{case_id}:{disposition}".encode()
    return _hmac.new(_hmac_secret, msg, hashlib.sha256).hexdigest()


def build_alert_card(
    decision: ModerationDecision,
    case_id: str | None,
    action_label: str,
) -> dict:
    """Return a cardsV2 entry for the initial moderation alert with disposition buttons."""
    content = decision.content
    time_str = content.received_at_utc.strftime("%Y-%m-%d %H:%M:%S UTC")

    detail_widgets: list[dict] = [
        _para(f"<b>Sender:</b> {content.sender_email}"),
        _para(f"<b>Space:</b> {content.space_name}"),
        _para(f"<b>Time:</b> {time_str}"),
    ]

    if decision.text_verdict.is_flagged:
        terms = ", ".join(decision.text_verdict.matched_terms[:10])
        line = f"<b>Text flag:</b> {decision.text_verdict.result.value} — <i>{terms}</i>"
        if decision.text_verdict.llm_rationale:
            line += f"\n<b>LLM:</b> {decision.text_verdict.llm_rationale}"
        detail_widgets.append(_para(line))

    worst_img = decision.worst_image_verdict
    if worst_img and worst_img.action != ModerationAction.PASS:
        score_str = str(worst_img.score) if worst_img.score is not None else "N/A"
        fmt = worst_img.image_format.value if worst_img.image_format else "unknown"
        detail_widgets.append(_para(
            f"<b>Image flag:</b> score={score_str}, action={worst_img.action.value}, format={fmt}"
        ))

    if case_id:
        detail_widgets.append(_para(f"<b>Case ID:</b> {case_id}"))

    sections: list[dict] = [{"widgets": detail_widgets}]

    if case_id:
        sections.append({
            "widgets": [{
                "buttonList": {
                    "buttons": [
                        _disposition_button("✅ True Positive", case_id, "true_positive"),
                        _disposition_button("❌ False Positive", case_id, "false_positive"),
                        _disposition_button("❓ Inconclusive", case_id, "inconclusive"),
                    ]
                }
            }]
        })

    return {
        "cardId": f"moderation_alert_{case_id or 'no_case'}",
        "card": {
            "header": {"title": f"🚨 Content Moderation — {action_label}"},
            "sections": sections,
        },
    }


def build_resolved_card(original_card_v2: dict, disposition: str, reviewer_display: str) -> dict:
    """Return a copy of the original card with the buttons section replaced by a resolution status."""
    label = _DISPOSITION_LABELS.get(disposition, disposition.upper())
    card = original_card_v2.get("card", {})
    sections = list(card.get("sections", []))

    # Drop the last section (buttons) and replace with resolution status
    if sections:
        sections = sections[:-1]
    sections.append({
        "widgets": [_para(f"<b>{label}</b> — closed by {reviewer_display}")]
    })

    return {
        "cardId": original_card_v2.get("cardId", "moderation_alert"),
        "card": {**card, "sections": sections},
    }


def _para(text: str) -> dict:
    return {"textParagraph": {"text": text}}


def _disposition_button(label: str, case_id: str, disposition: str) -> dict:
    token = _sign_disposition(case_id, disposition)
    url = (
        f"{_interaction_base_url}/disposition"
        f"?case_id={case_id}&disposition={disposition}&token={token}"
    )
    return {
        "text": label,
        "onClick": {"openLink": {"url": url}},
    }
