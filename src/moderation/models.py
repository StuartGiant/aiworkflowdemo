"""Content moderation data models.

All dataclasses are frozen (immutable) so they can be safely passed between
pipeline stages without risk of mutation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ModerationAction(str, Enum):
    """Final disposition for a piece of content."""

    PASS = "pass"
    REVIEW = "review"
    BLOCK = "block"


class TextVerdictResult(str, Enum):
    """Outcome of the two-layer text moderation pipeline."""

    PASS = "pass"
    TRUE_POSITIVE = "true_positive"        # keyword match, confirmed by LLM
    FALSE_POSITIVE = "false_positive"      # keyword match, overridden by LLM
    FLAGGED_FALLBACK = "flagged_fallback"  # keyword match, LLM unavailable


class ImageFormat(str, Enum):
    JPEG = "jpeg"
    BMP = "bmp"
    GIF = "gif"


# ---------------------------------------------------------------------------
# Layer verdicts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextVerdict:
    """Result of text moderation (keyword + optional LLM layer)."""

    result: TextVerdictResult
    matched_terms: tuple[str, ...] = field(default_factory=tuple)
    llm_rationale: Optional[str] = None

    @property
    def is_flagged(self) -> bool:
        return self.result in (
            TextVerdictResult.TRUE_POSITIVE,
            TextVerdictResult.FLAGGED_FALLBACK,
        )

    @property
    def action(self) -> ModerationAction:
        return ModerationAction.BLOCK if self.is_flagged else ModerationAction.PASS


@dataclass(frozen=True, slots=True)
class ImageVerdict:
    """Result of image moderation (Cloud Vision SafeSearch or local model)."""

    score: Optional[int]          # 0–100; None if API error / no image
    action: ModerationAction
    image_format: Optional[ImageFormat] = None
    frames_scored: int = 1        # > 1 for GIFs
    backend: str = "cloud_vision"


# ---------------------------------------------------------------------------
# Content item (input to the pipeline)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImageAttachment:
    """A single image attachment extracted from a Chat message."""

    data: bytes
    mime_type: str              # e.g. 'image/jpeg'
    filename: Optional[str] = None

    @property
    def image_format(self) -> ImageFormat:
        mt = self.mime_type.lower()
        if "gif" in mt:
            return ImageFormat.GIF
        if "bmp" in mt:
            return ImageFormat.BMP
        return ImageFormat.JPEG


@dataclass(frozen=True, slots=True)
class ContentItem:
    """Represents a single Google Chat message to be moderated."""

    message_name: str           # Chat API resource name, e.g. spaces/xxx/messages/yyy
    space_name: str
    sender_email: str
    text: Optional[str]
    images: tuple[ImageAttachment, ...] = field(default_factory=tuple)
    received_at_utc: datetime = field(default_factory=lambda: datetime.utcnow())


# ---------------------------------------------------------------------------
# Final decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModerationDecision:
    """Combined verdict for a ContentItem after all layers have run."""

    content: ContentItem
    text_verdict: TextVerdict
    image_verdicts: tuple[ImageVerdict, ...]   # one per attachment; empty if no images
    final_action: ModerationAction
    engine_version: str
    llm_model: Optional[str] = None
    vision_backend: str = "cloud_vision"

    @property
    def worst_image_verdict(self) -> Optional[ImageVerdict]:
        """Return the ImageVerdict with the highest-severity action, or None."""
        if not self.image_verdicts:
            return None
        priority = {ModerationAction.BLOCK: 2, ModerationAction.REVIEW: 1, ModerationAction.PASS: 0}
        return max(self.image_verdicts, key=lambda v: priority[v.action])
