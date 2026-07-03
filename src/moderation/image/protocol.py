"""ImageScorerBackend protocol — pluggable image moderation backend.

Phase 1: VisionAPIBackend (Google Cloud Vision SafeSearch)
Phase 2: LocalModelBackend (fine-tuned EfficientNet-B3) — swapped via config,
         no orchestrator changes required.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import ImageVerdict


@runtime_checkable
class ImageScorerBackend(Protocol):
    """Contract that every image scoring backend must implement."""

    def score(self, image_data: bytes, mime_type: str) -> ImageVerdict:
        """Score a single image (or GIF sequence).

        Args:
            image_data: Raw image bytes (BMP, JPEG, or GIF).
            mime_type:  MIME type string, e.g. 'image/jpeg'.

        Returns:
            ImageVerdict with a 0–100 score and resolved ModerationAction.
            If the backend cannot score the image (API error, unsupported
            format), it must return a safe fallback verdict rather than raise.
        """
        ...
