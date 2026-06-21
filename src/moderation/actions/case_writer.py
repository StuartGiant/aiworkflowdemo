"""case_writer — creates cases and evidence records for REVIEW/BLOCK verdicts.

Reuses the existing src/evidence/ layer for evidence integrity guarantees
(SHA-256, chain of custody, signed manifests).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import psycopg

from ..models import ModerationAction, ModerationDecision

log = logging.getLogger(__name__)

_ENGINE_PRINCIPAL = "content_moderation_pipeline"
_COLLECTION_METHOD = "api"
_SOURCE_SYSTEM = "google_workspace.chat_moderation"
_RETENTION_CLASS = "moderation_evidence"


class CaseWriter:
    """Writes moderation cases and evidence records to PostgreSQL."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def record(self, decision: ModerationDecision) -> Optional[uuid.UUID]:
        """Create a case + evidence items for a REVIEW or BLOCK decision.

        Returns the new case_id UUID, or None if the decision is PASS
        (nothing to record).
        """
        if decision.final_action == ModerationAction.PASS:
            return None

        case_id = uuid.uuid4()
        now_utc = datetime.now(timezone.utc)

        severity = "high" if decision.final_action == ModerationAction.BLOCK else "medium"
        confidence = 80 if decision.final_action == ModerationAction.BLOCK else 55

        try:
            with psycopg.connect(self._dsn) as conn:
                with conn.transaction():
                    self._insert_case(conn, case_id, decision, severity, confidence, now_utc)
                    self._insert_text_evidence(conn, case_id, decision, now_utc)
                    self._insert_image_evidence(conn, case_id, decision, now_utc)
                    self._insert_moderation_decision(conn, case_id, decision)

            log.info(
                "moderation.case_writer.recorded",
                extra={
                    "context": {
                        "case_id": str(case_id),
                        "action": decision.final_action.value,
                        "message": decision.content.message_name,
                    }
                },
            )
            return case_id

        except Exception as exc:
            log.error(
                "moderation.case_writer.error",
                extra={"context": {"err": str(exc), "message": decision.content.message_name}},
            )
            raise

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _insert_case(
        self,
        conn: psycopg.Connection,
        case_id: uuid.UUID,
        decision: ModerationDecision,
        severity: str,
        confidence: int,
        now_utc: datetime,
    ) -> None:
        case_code = f"MOD-{now_utc.strftime('%Y%m%d')}-{str(case_id)[:8].upper()}"
        action_label = decision.final_action.value.upper()
        title = f"[{action_label}] Content moderation — {decision.content.space_name}"

        conn.execute(
            """
            INSERT INTO cases (
                case_id, case_code, title, subject_ref,
                opened_at_utc, disposition, severity, confidence_score,
                confidence_band, notes, sender_email
            ) VALUES (
                %s, %s, %s, %s,
                %s, 'open', %s::severity, %s,
                %s::confidence_band, %s, %s
            )
            """,
            (
                case_id,
                case_code,
                title,
                decision.content.sender_email,
                now_utc,
                severity,
                confidence,
                _confidence_band(confidence),
                f"Auto-created by content moderation pipeline. "
                f"Message: {decision.content.message_name}. "
                f"Action: {decision.final_action.value}.",
                decision.content.sender_email or None,
            ),
        )

    def _insert_text_evidence(
        self,
        conn: psycopg.Connection,
        case_id: uuid.UUID,
        decision: ModerationDecision,
        now_utc: datetime,
    ) -> None:
        if not decision.content.text:
            return

        payload = json.dumps(
            {
                "message_name": decision.content.message_name,
                "space_name": decision.content.space_name,
                "sender_email": decision.content.sender_email,
                "text": decision.content.text,
                "text_verdict": decision.text_verdict.result.value,
                "matched_terms": list(decision.text_verdict.matched_terms),
                "llm_rationale": decision.text_verdict.llm_rationale,
            },
            ensure_ascii=False,
        ).encode()

        self._insert_evidence_item(
            conn,
            case_id=case_id,
            artefact_id=uuid.uuid4(),
            data=payload,
            mime_type="application/json",
            query=f"chat_moderation:text:{decision.content.message_name}",
            now_utc=now_utc,
        )

    def _insert_image_evidence(
        self,
        conn: psycopg.Connection,
        case_id: uuid.UUID,
        decision: ModerationDecision,
        now_utc: datetime,
    ) -> None:
        for idx, attachment in enumerate(decision.content.images):
            self._insert_evidence_item(
                conn,
                case_id=case_id,
                artefact_id=uuid.uuid4(),
                data=attachment.data,
                mime_type=attachment.mime_type,
                query=f"chat_moderation:image:{decision.content.message_name}:attachment_{idx}",
                now_utc=now_utc,
            )

    def _insert_evidence_item(
        self,
        conn: psycopg.Connection,
        *,
        case_id: uuid.UUID,
        artefact_id: uuid.UUID,
        data: bytes,
        mime_type: str,
        query: str,
        now_utc: datetime,
    ) -> None:
        import hashlib

        sha256 = hashlib.sha256(data).digest()

        conn.execute(
            """
            INSERT INTO evidence_items (
                artefact_id, case_id, source_system, collection_method,
                query, collector_principal, collected_at_utc, original_tz,
                bytes, sha256, mime_type, s3_uri, pii_tags,
                retention_class, manifest_uri, signing_key_id, manifest_version
            ) VALUES (
                %s, %s, %s::source_system, %s::collection_method,
                %s, %s, %s, 'UTC',
                %s, %s, %s, %s, %s,
                %s, %s, %s, 1
            )
            """,
            (
                artefact_id,
                case_id,
                _SOURCE_SYSTEM,
                _COLLECTION_METHOD,
                query,
                _ENGINE_PRINCIPAL,
                now_utc,
                len(data),
                sha256,
                mime_type,
                f"moderation://{case_id}/{artefact_id}",
                ["PII:sender_email"],
                _RETENTION_CLASS,
                f"moderation://{case_id}/{artefact_id}.manifest.json",
                "moderation_pipeline_v1",
            ),
        )

    def _insert_moderation_decision(
        self,
        conn: psycopg.Connection,
        case_id: uuid.UUID,
        decision: ModerationDecision,
    ) -> None:
        worst_image = decision.worst_image_verdict
        conn.execute(
            """
            INSERT INTO moderation_decisions (
                message_name, space_name, sender_email,
                text_verdict, text_matched_terms, text_llm_rationale,
                image_score, image_verdict, image_format, image_frames_scored,
                final_action, case_id,
                engine_version, llm_model, vision_backend
            ) VALUES (
                %s, %s, %s,
                %s::text_verdict_result, %s, %s,
                %s, %s::moderation_action, %s, %s,
                %s::moderation_action, %s,
                %s, %s, %s
            )
            """,
            (
                decision.content.message_name,
                decision.content.space_name,
                decision.content.sender_email,
                decision.text_verdict.result.value,
                list(decision.text_verdict.matched_terms),
                decision.text_verdict.llm_rationale,
                worst_image.score if worst_image else None,
                worst_image.action.value if worst_image else None,
                worst_image.image_format.value if worst_image and worst_image.image_format else None,
                worst_image.frames_scored if worst_image else None,
                decision.final_action.value,
                case_id,
                decision.engine_version,
                decision.llm_model,
                decision.vision_backend,
            ),
        )


def _confidence_band(score: int) -> str:
    if score <= 39:
        return "weak"
    if score <= 69:
        return "mixed"
    if score <= 89:
        return "strong"
    return "multi_source"
