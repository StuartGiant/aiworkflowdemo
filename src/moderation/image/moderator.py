"""Image moderation layer — wraps the pluggable backend."""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import ImageModerationConfig
from ..models import ImageAttachment, ImageVerdict
from .violence_detector import build_backend

log = logging.getLogger(__name__)


class ImageModerator:
    """Scores image attachments for violence using the configured backend."""

    def __init__(
        self,
        config: ImageModerationConfig,
        service_account_key_path: Path,
    ) -> None:
        self._backend = build_backend(
            backend_name=config.backend,
            service_account_key_path=service_account_key_path,
            config=config,
        )

    def moderate(self, attachment: ImageAttachment) -> ImageVerdict:
        """Score a single image attachment.

        Args:
            attachment: ImageAttachment containing raw bytes and MIME type.

        Returns:
            ImageVerdict. Always returns a verdict (never raises).
        """
        log.debug(
            "moderation.image.scoring",
            extra={
                "context": {
                    "mime_type": attachment.mime_type,
                    "filename": attachment.filename,
                    "bytes": len(attachment.data),
                }
            },
        )
        return self._backend.score(attachment.data, attachment.mime_type)
