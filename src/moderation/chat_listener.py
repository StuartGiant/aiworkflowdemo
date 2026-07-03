"""Google Chat Pub/Sub listener — subscribes to Chat Events and feeds the
moderation orchestrator.

Google Chat Events API publishes message-created events to a Pub/Sub topic
when a Chat app is subscribed to a space. This module pulls messages from
the configured subscription, decodes the event payload, fetches any image
attachments, and hands the ContentItem to ModerationOrchestrator.

Message flow:
  Pub/Sub pull → decode ChatEvent → build ContentItem → orchestrate → ack
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import AuthorizedSession, Request
from google.cloud import pubsub_v1
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import ModerationConfig
from .models import ContentItem, ImageAttachment
from .orchestrator import ModerationOrchestrator

log = logging.getLogger(__name__)

_CHAT_SCOPES = [
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.memberships.readonly",
]

_ADMIN_SCOPES = [
    "https://www.googleapis.com/auth/admin.directory.user.readonly",
]

_PULL_MAX_MESSAGES = 10
_PULL_TIMEOUT_SECONDS = 5.0
_ACK_DEADLINE_SECONDS = 60
_REACTIVATE_INTERVAL_SECONDS = 300  # reactivate Workspace Events subscription every 5 min

# Supported image MIME types
_IMAGE_MIME_TYPES = {"image/jpeg", "image/bmp", "image/gif", "image/png"}


class ChatListener:
    """Pulls Google Chat Events from Pub/Sub and invokes the orchestrator."""

    def __init__(
        self,
        config: ModerationConfig,
        orchestrator: ModerationOrchestrator,
        admin_email: str,
    ) -> None:
        self._config = config
        self._orchestrator = orchestrator

        # Pub/Sub subscriber client
        sa_credentials = service_account.Credentials.from_service_account_file(
            str(config.service_account_key_path),
            scopes=["https://www.googleapis.com/auth/pubsub"],
        )
        self._subscriber = pubsub_v1.SubscriberClient(credentials=sa_credentials)
        self._subscription_path = self._subscriber.subscription_path(
            config.pubsub.project_id,
            config.pubsub.subscription_id,
        )

        # Chat API client for fetching full message content and attachments
        chat_credentials = (
            service_account.Credentials.from_service_account_file(
                str(config.service_account_key_path),
                scopes=_CHAT_SCOPES,
            ).with_subject(admin_email)
        )
        self._chat_svc = build(
            "chat", "v1", credentials=chat_credentials, cache_discovery=False
        )

        # Admin Directory API client for resolving user IDs to email addresses
        admin_credentials = (
            service_account.Credentials.from_service_account_file(
                str(config.service_account_key_path),
                scopes=_ADMIN_SCOPES,
            ).with_subject(admin_email)
        )
        self._directory_svc = build(
            "admin", "directory_v1", credentials=admin_credentials, cache_discovery=False
        )

        # Authorized session for Workspace Events API reactivation calls
        self._workspace_events_session = AuthorizedSession(chat_credentials)

        # Bot session authenticated as the service account directly (no DWD) —
        # required for Chat media download; DWD user credentials get 400.
        bot_credentials = service_account.Credentials.from_service_account_file(
            str(config.service_account_key_path),
            scopes=["https://www.googleapis.com/auth/chat.bot"],
        )
        self._bot_session = AuthorizedSession(bot_credentials)

        # Sender email cache: user resource name → email
        self._sender_email_cache: dict[str, str] = {}

        # Mutable sub name — updated in-place if the subscription is recreated
        self._workspace_events_sub_name: str = (
            config.pubsub.workspace_events_subscription_name
        )

        # Track last reactivation time for periodic auto-reactivation
        self._last_reactivation: float = time.monotonic()

        log.info(
            "moderation.chat_listener.init",
            extra={
                "context": {
                    "project": config.pubsub.project_id,
                    "subscription": config.pubsub.subscription_id,
                }
            },
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_forever(self, poll_interval_seconds: float = 1.0) -> None:
        """Block and process Pub/Sub messages indefinitely.

        Catches and logs all exceptions per message so one bad payload
        does not halt the listener loop.
        """
        log.info("moderation.chat_listener.started")

        while True:
            try:
                self._pull_and_process()
            except KeyboardInterrupt:
                log.info("moderation.chat_listener.stopping")
                break
            except Exception as exc:
                err_str = str(exc)
                # 504 Deadline Exceeded is a normal empty-poll timeout, not an error
                if "504" in err_str or "Deadline Exceeded" in err_str:
                    log.debug("moderation.chat_listener.poll_empty")
                else:
                    log.error(
                        "moderation.chat_listener.poll_error",
                        extra={"context": {"err": err_str}},
                    )

            if (time.monotonic() - self._last_reactivation) >= _REACTIVATE_INTERVAL_SECONDS:
                self._reactivate_subscription()
                self._last_reactivation = time.monotonic()

            time.sleep(poll_interval_seconds)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _pull_and_process(self) -> None:
        response = self._subscriber.pull(
            request={
                "subscription": self._subscription_path,
                "max_messages": _PULL_MAX_MESSAGES,
            },
            timeout=_PULL_TIMEOUT_SECONDS,
        )

        if not response.received_messages:
            return

        ack_ids: list[str] = []

        for received_msg in response.received_messages:
            ack_id = received_msg.ack_id
            pubsub_msg = received_msg.message
            run_id = uuid.uuid4()

            try:
                content = self._decode_message(pubsub_msg)
                if content is not None:
                    log.info(
                        "moderation.chat_listener.processing",
                        extra={
                            "context": {
                                "run_id": str(run_id),
                                "message": content.message_name,
                            }
                        },
                    )
                    self._orchestrator.moderate(content)
                ack_ids.append(ack_id)

            except Exception as exc:
                log.error(
                    "moderation.chat_listener.message_error",
                    extra={
                        "context": {
                            "run_id": str(run_id),
                            "err": str(exc),
                            "msg_id": pubsub_msg.message_id,
                        }
                    },
                )
                # Still ack to avoid infinite redelivery of unparseable messages.
                ack_ids.append(ack_id)

        if ack_ids:
            self._subscriber.acknowledge(
                request={"subscription": self._subscription_path, "ack_ids": ack_ids}
            )

    def _decode_message(self, pubsub_msg) -> Optional[ContentItem]:
        """Decode a Pub/Sub message into a ContentItem.

        Handles CloudEvents format delivered by the Workspace Events API:
        - Event type is in the ce-type Pub/Sub attribute, not the body
        - Body contains a partial Message resource; full content is fetched
          from the Chat API when the payload is minimal.

        Returns None for non-message events (silently ignored).
        """
        raw_data = pubsub_msg.data.decode("utf-8")
        event: dict = json.loads(raw_data)

        # Workspace Events API delivers CloudEvents — type is in attributes
        ce_type = pubsub_msg.attributes.get("ce-type", "")
        if ce_type != "google.workspace.chat.message.v1.created":
            log.info(
                "moderation.chat_listener.skip_event",
                extra={"context": {"type": ce_type, "attrs": dict(pubsub_msg.attributes)}},
            )
            return None

        message: dict = event.get("message", {})
        message_name: str = message.get("name", "")
        # Derive space name from message name (spaces/{id}/messages/{id})
        space_name: str = "/".join(message_name.split("/")[:2]) if message_name else ""
        sender_email: str = (
            message.get("sender", {}).get("email", "")
            or message.get("sender", {}).get("name", "")
        )
        text: str = message.get("text", "") or message.get("argumentText", "")

        # Workspace Events API often delivers a minimal payload with only the
        # message name. Fetch the full message from Chat API to get text/sender.
        if message_name and (not sender_email or text is None):
            full_msg = self._fetch_full_message(message_name)
            if full_msg:
                sender_email = (
                    full_msg.get("sender", {}).get("email", "")
                    or full_msg.get("sender", {}).get("name", "")
                ) or sender_email
                text = full_msg.get("text", "") or full_msg.get("argumentText", "") or text
                message = full_msg

        # Resolve user resource name (users/NNNNN) to email if needed
        if sender_email and not "@" in sender_email:
            sender_email = self._resolve_sender_email(sender_email)

        create_time_str: str = message.get("createTime", "")
        received_at = (
            datetime.fromisoformat(create_time_str.replace("Z", "+00:00"))
            if create_time_str
            else datetime.now(timezone.utc)
        )

        images = self._fetch_attachments(message)

        return ContentItem(
            message_name=message_name,
            space_name=space_name,
            sender_email=sender_email,
            text=text if text else None,
            images=tuple(images),
            received_at_utc=received_at,
        )

    def _fetch_full_message(self, message_name: str) -> dict:
        """Fetch the full Message resource from the Chat API."""
        try:
            return (
                self._chat_svc.spaces()
                .messages()
                .get(name=message_name)
                .execute()
            )
        except HttpError as exc:
            log.warning(
                "moderation.chat_listener.full_message_fetch_failed",
                extra={"context": {"message": message_name, "status": exc.resp.status}},
            )
            return {}

    def _resolve_sender_email(self, sender_ref: str) -> str:
        """Resolve a user resource name (users/NNNNN) to an email address.

        Results are cached for the lifetime of the listener to avoid repeated
        Directory API calls for the same sender.
        """
        if sender_ref in self._sender_email_cache:
            return self._sender_email_cache[sender_ref]

        user_id = sender_ref.split("/")[-1]
        try:
            result = self._directory_svc.users().get(userKey=user_id).execute()
            email = result.get("primaryEmail", sender_ref)
            self._sender_email_cache[sender_ref] = email
            return email
        except HttpError as exc:
            log.warning(
                "moderation.chat_listener.sender_resolve_failed",
                extra={"context": {"sender": sender_ref, "status": exc.resp.status}},
            )
            return sender_ref
        except Exception as exc:
            log.warning(
                "moderation.chat_listener.sender_resolve_error",
                extra={"context": {"sender": sender_ref, "err": str(exc)[:120]}},
            )
            return sender_ref

    def _reactivate_subscription(self) -> None:
        """Renew, reactivate, or recreate the Workspace Events subscription.

        Google Chat message subscriptions expire after a maximum of 24 hours.
        This method is called every _REACTIVATE_INTERVAL_SECONDS and:
          - PATCHes the TTL back to 24 h when the subscription is ACTIVE
          - POSTs :reactivate when it is SUSPENDED
          - Recreates it from scratch when it is gone (403 / 404)
        """
        sub_name = self._workspace_events_sub_name
        if not sub_name:
            return
        try:
            resp = self._workspace_events_session.get(
                f"https://workspaceevents.googleapis.com/v1/{sub_name}"
            )

            if resp.status_code == 200:
                state = resp.json().get("state", "")
                if state == "ACTIVE":
                    self._renew_subscription_ttl(sub_name)
                elif state == "SUSPENDED":
                    reactivate = self._workspace_events_session.post(
                        f"https://workspaceevents.googleapis.com/v1/{sub_name}:reactivate",
                        json={},
                    )
                    if reactivate.status_code == 200:
                        log.info(
                            "moderation.chat_listener.subscription_reactivated",
                            extra={"context": {"subscription": sub_name}},
                        )
                    else:
                        log.warning(
                            "moderation.chat_listener.reactivation_failed",
                            extra={"context": {"status": reactivate.status_code, "subscription": sub_name}},
                        )
                else:
                    log.warning(
                        "moderation.chat_listener.subscription_unexpected_state",
                        extra={"context": {"state": state, "subscription": sub_name}},
                    )

            elif resp.status_code in (403, 404):
                log.warning(
                    "moderation.chat_listener.subscription_gone",
                    extra={"context": {"status": resp.status_code, "subscription": sub_name}},
                )
                self._recreate_subscription()

            else:
                log.warning(
                    "moderation.chat_listener.subscription_check_failed",
                    extra={"context": {"status": resp.status_code, "subscription": sub_name}},
                )

        except Exception as exc:
            log.warning(
                "moderation.chat_listener.reactivation_error",
                extra={"context": {"err": str(exc)}},
            )

    def _renew_subscription_ttl(self, sub_name: str) -> None:
        """PATCH the subscription TTL back to the maximum (24 h) to prevent expiry."""
        resp = self._workspace_events_session.patch(
            f"https://workspaceevents.googleapis.com/v1/{sub_name}",
            params={"updateMask": "ttl"},
            json={"ttl": "86400s"},
        )
        if resp.status_code == 200:
            log.info(
                "moderation.chat_listener.subscription_renewed",
                extra={"context": {"subscription": sub_name}},
            )
        else:
            log.warning(
                "moderation.chat_listener.renewal_failed",
                extra={"context": {"status": resp.status_code, "subscription": sub_name}},
            )

    def _recreate_subscription(self) -> None:
        """Delete (if present) and recreate the Workspace Events subscription."""
        target = self._config.pubsub.workspace_events_target_resource
        topic = (
            f"projects/{self._config.pubsub.project_id}"
            f"/topics/{self._config.pubsub.topic_id}"
        )
        if not target or not self._config.pubsub.topic_id:
            log.error(
                "moderation.chat_listener.recreation_skipped",
                extra={"context": {"reason": "WORKSPACE_EVENTS_TARGET_RESOURCE or PUBSUB_TOPIC_ID not set"}},
            )
            return
        try:
            resp = self._workspace_events_session.post(
                "https://workspaceevents.googleapis.com/v1/subscriptions",
                json={
                    "targetResource": target,
                    "eventTypes": ["google.workspace.chat.message.v1.created"],
                    "notificationEndpoint": {"pubsubTopic": topic},
                    "payloadOptions": {"includeResource": True},
                },
            )
            if resp.status_code == 200:
                new_name = (
                    resp.json()
                    .get("response", resp.json())
                    .get("name", self._workspace_events_sub_name)
                )
                self._workspace_events_sub_name = new_name
                log.info(
                    "moderation.chat_listener.subscription_recreated",
                    extra={"context": {"subscription": new_name}},
                )
            else:
                log.error(
                    "moderation.chat_listener.recreation_failed",
                    extra={"context": {"status": resp.status_code, "body": resp.text[:300]}},
                )
        except Exception as exc:
            log.error(
                "moderation.chat_listener.recreation_error",
                extra={"context": {"err": str(exc)}},
            )

    def _fetch_attachments(self, message: dict) -> list[ImageAttachment]:
        """Download image attachments from the Chat API.

        The Chat API media.download endpoint requires attachmentDataRef.resourceName
        (a proto-encoded blob) as the URL path — NOT the human-readable attachment
        name. The blob must be URL-encoded (= signs → %3D) in the path.
        DWD user credentials (not bot credentials) are required.
        """
        attachments: list[ImageAttachment] = []

        for attachment in message.get("attachment", []):
            content_type: str = attachment.get("contentType", "")
            if content_type not in _IMAGE_MIME_TYPES:
                continue

            attachment_name: str = attachment.get("name", "")
            resource_name: str = (
                attachment.get("attachmentDataRef", {}).get("resourceName", "")
            )
            if not resource_name:
                continue

            try:
                encoded_rn = urllib.parse.quote(resource_name, safe="/")
                url = f"https://chat.googleapis.com/v1/media/{encoded_rn}?alt=media"
                resp = self._workspace_events_session.get(url)
                resp.raise_for_status()
                data = resp.content
                if data:
                    attachments.append(
                        ImageAttachment(
                            data=data,
                            mime_type=content_type,
                            filename=attachment_name,
                        )
                    )
            except Exception as exc:
                log.warning(
                    "moderation.chat_listener.attachment_fetch_failed",
                    extra={
                        "context": {
                            "attachment": attachment_name,
                            "err": str(exc)[:120],
                        }
                    },
                )

        return attachments
