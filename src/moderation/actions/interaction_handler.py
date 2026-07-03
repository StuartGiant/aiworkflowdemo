"""Google Chat interaction handler — processes reviewer button clicks.

Google Chat POSTs CARD_CLICKED events to the bot's configured HTTP endpoint
when a user clicks a button in a card. This module serves that endpoint,
updates the case disposition in the DB, and returns an updated card.

Setup:
  1. Expose this server publicly (e.g. ngrok http 8080)
  2. In GCP Console → APIs & Services → Google Chat API → Configuration,
     set App URL to: https://<your-ngrok-id>.ngrok.io/chat/interactions
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import logging
import uuid
from pathlib import Path

import psycopg
from flask import Flask, jsonify, request
from google.auth.transport import requests as grequests
from google.oauth2 import id_token, service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .card_builder import build_resolved_card

log = logging.getLogger(__name__)

_VALID_DISPOSITIONS = {"true_positive", "false_positive", "inconclusive"}
_TOMBSTONE = "🚫 This content has been removed by the Security Content Moderation system."
_DWD_SCOPES = [
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
]

_CHAT_ISSUER = "chat@system.gserviceaccount.com"
_CHAT_CERT_URL = f"https://www.googleapis.com/service_accounts/v1/jwk/{_CHAT_ISSUER}"


def _verify_disposition_token(secret: bytes, case_id: str, disposition: str, token: str) -> bool:
    """Timing-safe HMAC-SHA256 verification of a disposition link token."""
    expected = _hmac.new(secret, f"{case_id}:{disposition}".encode(), hashlib.sha256).hexdigest()
    return _hmac.compare_digest(expected, token)


def _verify_chat_jwt(bearer_token: str, audience: str) -> bool:
    """Verify a Google Chat service-account Bearer JWT and its audience claim."""
    try:
        claims = id_token.verify_token(
            bearer_token,
            grequests.Request(),
            audience=audience,
            certs_url=_CHAT_CERT_URL,
        )
        return claims.get("iss") == _CHAT_ISSUER
    except Exception:
        return False


def create_app(
    db_dsn: str,
    sa_key_path: Path,
    admin_email: str,
    hmac_secret: bytes,
    chat_audience: str,
) -> Flask:
    """Create the Flask app with DB and Chat API credentials."""
    app = Flask(__name__)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    @app.get("/disposition")
    def disposition_link():
        """Handles reviewer button clicks via browser link (openLink buttons)."""
        case_id_str = request.args.get("case_id", "")
        disposition = request.args.get("disposition", "")
        token = request.args.get("token", "")

        if not case_id_str or disposition not in _VALID_DISPOSITIONS:
            return "<h2>❌ Invalid request.</h2>", 400

        if not token or not _verify_disposition_token(hmac_secret, case_id_str, disposition, token):
            log.warning("moderation.interaction.invalid_token",
                        extra={"context": {"case_id": case_id_str}})
            return "<h2>❌ Invalid or missing token.</h2>", 403

        try:
            case_id = uuid.UUID(case_id_str)
        except ValueError:
            return "<h2>❌ Invalid case ID.</h2>", 400

        try:
            _update_disposition(db_dsn, case_id, disposition)
        except Exception as exc:
            log.error("moderation.interaction.db_error",
                      extra={"context": {"case_id": case_id_str, "err": str(exc)}})
            return "<h2>❌ Failed to update case. Please try again.</h2>", 500

        log.info(
            "moderation.interaction.disposition_updated",
            extra={"context": {"case_id": case_id_str, "disposition": disposition}},
        )

        # If marked true positive, tombstone the original message
        if disposition == "true_positive":
            message_name = _get_message_name(db_dsn, case_id)
            if message_name:
                _tombstone_message(message_name, sa_key_path, admin_email)

        label_map = {
            "true_positive": "✅ True Positive",
            "false_positive": "❌ False Positive",
            "inconclusive": "❓ Inconclusive",
        }
        label = label_map[disposition]
        return f"""<!DOCTYPE html>
<html><head><title>Case Updated</title>
<style>body{{font-family:sans-serif;text-align:center;padding:60px;}}
h2{{color:#2d7d2d;}}</style></head>
<body>
<h2>{label}</h2>
<p>Case <code>{case_id_str[:8]}…</code> has been marked as <strong>{disposition.replace('_',' ').upper()}</strong>.</p>
<p style="color:#888;font-size:0.85em">This tab will close in 3 seconds.</p>
<script>setTimeout(()=>window.close(),3000);</script>
</body></html>""", 200

    @app.get("/chat/interactions")
    def interactions_verify():
        return jsonify({"status": "ok"}), 200

    @app.post("/chat/interactions")
    def interactions():
        bearer = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not bearer or not _verify_chat_jwt(bearer, chat_audience):
            log.warning("moderation.interaction.jwt_rejected",
                        extra={"context": {"remote": request.remote_addr}})
            return jsonify({"error": "Unauthorized"}), 401

        log.info(
            "moderation.interaction.received",
            extra={"context": {"body": str(request.data[:500])}},
        )
        payload = request.get_json(silent=True) or {}
        event_type = payload.get("type", "")

        if event_type != "CARD_CLICKED":
            return jsonify({}), 200

        action = payload.get("action", {})
        fn = action.get("actionMethodName", "")
        if fn != "update_disposition":
            return jsonify({}), 200

        params = {p["key"]: p["value"] for p in action.get("parameters", [])}
        case_id_str = params.get("case_id", "")
        disposition = params.get("disposition", "")

        if not case_id_str or disposition not in _VALID_DISPOSITIONS:
            log.warning("moderation.interaction.invalid_params",
                        extra={"context": {"params": params}})
            return jsonify({"text": "Invalid request parameters."}), 400

        try:
            case_id = uuid.UUID(case_id_str)
        except ValueError:
            return jsonify({"text": "Invalid case ID format."}), 400

        user = payload.get("user", {})
        reviewer_display = user.get("displayName") or user.get("name", "reviewer")

        try:
            _update_disposition(db_dsn, case_id, disposition)
        except Exception as exc:
            log.error("moderation.interaction.db_error",
                      extra={"context": {"case_id": case_id_str, "err": str(exc)}})
            return jsonify({"text": "Failed to update case. Please try again."}), 500

        if disposition == "true_positive":
            message_name = _get_message_name(db_dsn, case_id)
            if message_name:
                _tombstone_message(message_name, sa_key_path, admin_email)

        log.info(
            "moderation.interaction.disposition_updated",
            extra={"context": {"case_id": case_id_str, "disposition": disposition,
                               "by": reviewer_display}},
        )

        existing_cards = payload.get("message", {}).get("cardsV2", [])
        if existing_cards:
            updated_card = build_resolved_card(existing_cards[0], disposition, reviewer_display)
            return jsonify({
                "actionResponse": {"type": "UPDATE_MESSAGE"},
                "cardsV2": [updated_card],
            })

        return jsonify({
            "actionResponse": {"type": "NEW_MESSAGE"},
            "text": (
                f"Case `{case_id_str}` marked as *{disposition.replace('_', ' ').upper()}* "
                f"by {reviewer_display}."
            ),
        })

    return app


def _update_disposition(dsn: str, case_id: uuid.UUID, disposition: str) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "UPDATE cases SET disposition = %s::disposition WHERE case_id = %s",
            (disposition, case_id),
        )
        conn.commit()


def _get_message_name(dsn: str, case_id: uuid.UUID) -> str | None:
    """Look up the Chat message name from moderation_decisions for this case."""
    try:
        with psycopg.connect(dsn) as conn:
            row = conn.execute(
                "SELECT message_name FROM moderation_decisions WHERE case_id = %s LIMIT 1",
                (case_id,),
            ).fetchone()
        return row[0] if row else None
    except Exception as exc:
        log.error("moderation.interaction.message_lookup_error",
                  extra={"context": {"case_id": str(case_id), "err": str(exc)}})
        return None


def _tombstone_message(message_name: str, sa_key_path: Path, admin_email: str) -> None:
    """Patch the Chat message with the tombstone text and clear attachments."""
    try:
        creds = (
            service_account.Credentials.from_service_account_file(
                str(sa_key_path), scopes=_DWD_SCOPES
            ).with_subject(admin_email)
        )
        svc = build("chat", "v1", credentials=creds, cache_discovery=False)
        svc.spaces().messages().patch(
            name=message_name,
            updateMask="text,attachment",
            body={"text": _TOMBSTONE, "attachment": []},
        ).execute()
        log.info("moderation.interaction.message_tombstoned",
                 extra={"context": {"message": message_name}})
    except HttpError as exc:
        log.error("moderation.interaction.tombstone_failed",
                  extra={"context": {"message": message_name, "status": exc.resp.status,
                                     "err": str(exc)}})
    except Exception as exc:
        log.error("moderation.interaction.tombstone_error",
                  extra={"context": {"message": message_name, "err": str(exc)}})
