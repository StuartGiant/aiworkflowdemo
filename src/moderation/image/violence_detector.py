"""Image violence detection backends.

Phase 1 — VisionAPIBackend:
    Uses Google Cloud Vision SafeSearch to score images for violence.
    Supports BMP, JPEG, GIF (multi-frame via Pillow frame sampling).

Phase 2 — LocalModelBackend (stub):
    Placeholder for the future fine-tuned EfficientNet-B3 model.
    Raises NotImplementedError until Phase 2 is built.

Backend selection is controlled by config.image.backend:
    "cloud_vision"  → VisionAPIBackend   (Phase 1, default)
    "local_model"   → LocalModelBackend  (Phase 2, future)
"""

from __future__ import annotations

import base64
import io
import logging
from pathlib import Path
from typing import Optional

try:
    from google.cloud import vision as _vision_module
    from google.oauth2 import service_account as _sa_module
except ImportError:  # pragma: no cover — only missing in test sandbox
    _vision_module = None  # type: ignore[assignment]
    _sa_module = None  # type: ignore[assignment]

from ..config import ImageModerationConfig
from ..models import ImageFormat, ImageVerdict, ModerationAction

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SafeSearch likelihood → 0-100 score mapping (Phase 1)
# ---------------------------------------------------------------------------

_LIKELIHOOD_SCORE: dict[str, int] = {
    "VERY_UNLIKELY": 10,
    "UNLIKELY": 30,
    "POSSIBLE": 60,
    "LIKELY": 80,
    "VERY_LIKELY": 95,
    "UNKNOWN": 55,  # treated conservatively
}


def _score_to_action(score: int) -> ModerationAction:
    if score <= 50:
        return ModerationAction.PASS
    if score <= 70:
        return ModerationAction.REVIEW
    return ModerationAction.BLOCK


def _detect_format(mime_type: str) -> ImageFormat:
    mt = mime_type.lower()
    if "gif" in mt:
        return ImageFormat.GIF
    if "bmp" in mt:
        return ImageFormat.BMP
    return ImageFormat.JPEG


# ---------------------------------------------------------------------------
# Phase 1: Google Cloud Vision SafeSearch
# ---------------------------------------------------------------------------


class VisionAPIBackend:
    """Score images using Google Cloud Vision SafeSearch API."""

    def __init__(
        self,
        service_account_key_path: Path,
        config: ImageModerationConfig,
    ) -> None:
        vision = _vision_module
        service_account = _sa_module

        credentials = service_account.Credentials.from_service_account_file(
            str(service_account_key_path),
            scopes=["https://www.googleapis.com/auth/cloud-vision"],
        )
        self._client = vision.ImageAnnotatorClient(credentials=credentials)
        self._config = config

    def score(self, image_data: bytes, mime_type: str) -> ImageVerdict:
        """Score image_data for violence. Returns a safe fallback on API error."""
        fmt = _detect_format(mime_type)

        if fmt == ImageFormat.GIF:
            return self._score_gif(image_data)
        return self._score_single(image_data, fmt, frames_scored=1)

    def _score_single(
        self, data: bytes, fmt: ImageFormat, frames_scored: int
    ) -> ImageVerdict:
        from google.cloud import vision

        try:
            vision = _vision_module
            image = vision.Image(content=data)
            response = self._client.safe_search_detection(image=image)

            if response.error.message:
                raise RuntimeError(response.error.message)

            likelihood_name = response.safe_search_annotation.violence.name
            score = _LIKELIHOOD_SCORE.get(likelihood_name, 55)

            log.info(
                "moderation.image.scored",
                extra={
                    "context": {
                        "likelihood": likelihood_name,
                        "score": score,
                        "format": fmt.value,
                        "frames_scored": frames_scored,
                        "backend": "cloud_vision",
                    }
                },
            )

            return ImageVerdict(
                score=score,
                action=_score_to_action(score),
                image_format=fmt,
                frames_scored=frames_scored,
                backend="cloud_vision",
            )

        except Exception as exc:
            log.warning(
                "moderation.image.api_error",
                extra={"context": {"err": str(exc), "backend": "cloud_vision"}},
            )
            return self._fallback_verdict(fmt, frames_scored)

    def _score_gif(self, data: bytes) -> ImageVerdict:
        """Sample frames from a GIF and return the worst-case score."""
        try:
            from PIL import Image as PILImage

            gif = PILImage.open(io.BytesIO(data))
            fps = self._config.gif_frame_sample_fps
            max_frames = self._config.gif_max_frames

            frames_to_score: list[bytes] = []
            frame_idx = 0

            try:
                while True:
                    # Sample at configured fps (default: every frame for low-fps GIFs)
                    duration_ms = gif.info.get("duration", 100)  # ms per frame
                    frames_per_sample = max(1, int(1000 / (fps * duration_ms)))

                    if frame_idx % frames_per_sample == 0:
                        buf = io.BytesIO()
                        gif.convert("RGB").save(buf, format="JPEG")
                        frames_to_score.append(buf.getvalue())

                    if len(frames_to_score) >= max_frames:
                        break

                    frame_idx += 1
                    gif.seek(gif.tell() + 1)

            except EOFError:
                pass  # end of GIF frames

        except Exception as exc:
            log.warning(
                "moderation.image.gif_extract_error",
                extra={"context": {"err": str(exc)}},
            )
            return self._fallback_verdict(ImageFormat.GIF, frames_scored=0)

        if not frames_to_score:
            return self._fallback_verdict(ImageFormat.GIF, frames_scored=0)

        # Score each sampled frame; take the maximum (worst case)
        worst: Optional[ImageVerdict] = None
        for frame_data in frames_to_score:
            verdict = self._score_single(frame_data, ImageFormat.GIF, frames_scored=1)
            if worst is None or (verdict.score or 0) > (worst.score or 0):
                worst = verdict

        assert worst is not None
        return ImageVerdict(
            score=worst.score,
            action=worst.action,
            image_format=ImageFormat.GIF,
            frames_scored=len(frames_to_score),
            backend="cloud_vision",
        )

    def _fallback_verdict(self, fmt: ImageFormat, frames_scored: int) -> ImageVerdict:
        """Return a safe fallback when the API cannot be reached."""
        fallback_action = (
            ModerationAction.BLOCK
            if self._config.fallback_on_api_error == "block"
            else ModerationAction.REVIEW
        )
        return ImageVerdict(
            score=None,
            action=fallback_action,
            image_format=fmt,
            frames_scored=frames_scored,
            backend="cloud_vision",
        )


# ---------------------------------------------------------------------------
# Phase 2 stub: local fine-tuned EfficientNet-B3
# ---------------------------------------------------------------------------


class LocalModelBackend:
    """Phase 2 placeholder — local violence detection model.

    Not yet implemented. See ADR 0007 for the migration plan and trigger
    criteria.  Switch to this backend by setting
    config.image_moderation.backend = "local_model" in
    config/content_moderation.yml.
    """

    def __init__(self, config: ImageModerationConfig) -> None:
        raise NotImplementedError(
            "LocalModelBackend is not yet implemented. "
            "See ADR 0007 Phase 2 for the implementation plan. "
            "Use backend='cloud_vision' until Phase 2 criteria are met."
        )

    def score(self, image_data: bytes, mime_type: str) -> ImageVerdict:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_backend(
    backend_name: str,
    service_account_key_path: Path,
    config: ImageModerationConfig,
) -> VisionAPIBackend:
    """Instantiate the configured image scoring backend."""
    if backend_name == "cloud_vision":
        return VisionAPIBackend(
            service_account_key_path=service_account_key_path,
            config=config,
        )
    if backend_name == "local_model":
        return LocalModelBackend(config=config)  # type: ignore[return-value]
    raise ValueError(
        f"Unknown image moderation backend: {backend_name!r}. "
        "Expected 'cloud_vision' or 'local_model'."
    )
