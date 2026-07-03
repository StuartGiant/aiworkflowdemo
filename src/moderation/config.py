"""Content moderation configuration.

All secrets are read from environment variables (never from YAML).
Structural/behavioural settings come from config/content_moderation.yml.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


# ---------------------------------------------------------------------------
# Sub-configs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LLMConfig:
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 256
    timeout_seconds: int = 10


@dataclass(frozen=True, slots=True)
class TextModerationConfig:
    hard_block_keyword_list_paths: tuple[Path, ...] = field(default_factory=tuple)
    soft_flag_keyword_list_paths: tuple[Path, ...] = field(default_factory=tuple)
    llm: LLMConfig = field(default_factory=LLMConfig)


@dataclass(frozen=True, slots=True)
class ImageModerationConfig:
    backend: str = "cloud_vision"          # "cloud_vision" | "local_model"
    gif_frame_sample_fps: int = 1
    gif_max_frames: int = 30
    fallback_on_api_error: str = "review"  # "review" | "block"


@dataclass(frozen=True, slots=True)
class ActionsConfig:
    reviewer_email: str = ""
    reviewer_chat_user_id: str = ""


@dataclass(frozen=True, slots=True)
class PubSubConfig:
    project_id: str = ""
    subscription_id: str = ""
    topic_id: str = ""                             # Pub/Sub topic name (for sub recreation)
    workspace_events_subscription_name: str = ""   # for auto-renewal / recreation
    workspace_events_target_resource: str = ""     # e.g. //chat.googleapis.com/spaces/XXXXX


@dataclass(frozen=True, slots=True)
class InteractionServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass(frozen=True, slots=True)
class ModerationConfig:
    text: TextModerationConfig
    image: ImageModerationConfig
    actions: ActionsConfig
    pubsub: PubSubConfig
    interaction_server: InteractionServerConfig
    # Secrets from env — never stored in this object as-is; accessed via helpers
    _anthropic_api_key: str = field(repr=False, default="")
    _service_account_key_path: str = field(repr=False, default="")

    @property
    def anthropic_api_key(self) -> str:
        return self._anthropic_api_key

    @property
    def service_account_key_path(self) -> Path:
        return Path(self._service_account_key_path)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_config(config_path: Optional[Path] = None) -> ModerationConfig:
    """Load ModerationConfig from YAML file + environment variables.

    Args:
        config_path: Path to content_moderation.yml. Defaults to
                     <project_root>/config/content_moderation.yml.
    """
    if config_path is None:
        config_path = Path(__file__).resolve().parents[2] / "config" / "content_moderation.yml"

    if not config_path.exists():
        raise FileNotFoundError(
            f"Content moderation config not found: {config_path}. "
            "Copy config/content_moderation.yml.example and populate it."
        )

    with config_path.open() as fh:
        raw: dict = yaml.safe_load(fh) or {}

    text_raw = raw.get("text_moderation", {})
    llm_raw = text_raw.get("llm", {})
    image_raw = raw.get("image_moderation", {})
    actions_raw = raw.get("actions", {})
    pubsub_raw = raw.get("pubsub", {})

    # Resolve keyword list paths relative to project root (config file's parent)
    project_root = config_path.parent.parent
    hard_block_paths = tuple(
        (project_root / p).resolve()
        for p in text_raw.get("hard_block_keyword_lists", [])
    )
    soft_flag_paths = tuple(
        (project_root / p).resolve()
        for p in text_raw.get("soft_flag_keyword_lists", [])
    )

    # Secrets from env
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    sa_key_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY_PATH", "")
    reviewer_email = (
        os.environ.get("MODERATION_REVIEWER_EMAIL", "")
        or actions_raw.get("reviewer_email", "")
    )
    reviewer_chat_user_id = (
        os.environ.get("MODERATION_REVIEWER_CHAT_USER_ID", "")
        or actions_raw.get("reviewer_chat_user_id", "")
    )
    pubsub_project = os.environ.get("PUBSUB_PROJECT_ID", "")
    pubsub_sub = os.environ.get("PUBSUB_SUBSCRIPTION_ID", "")
    pubsub_topic = os.environ.get("PUBSUB_TOPIC_ID", "")
    workspace_events_sub = os.environ.get("WORKSPACE_EVENTS_SUBSCRIPTION_NAME", "")
    workspace_events_target = os.environ.get("WORKSPACE_EVENTS_TARGET_RESOURCE", "")

    interaction_raw = raw.get("interaction_server", {})

    return ModerationConfig(
        text=TextModerationConfig(
            hard_block_keyword_list_paths=hard_block_paths,
            soft_flag_keyword_list_paths=soft_flag_paths,
            llm=LLMConfig(
                model=llm_raw.get("model", "claude-sonnet-4-6"),
                max_tokens=int(llm_raw.get("max_tokens", 256)),
                timeout_seconds=int(llm_raw.get("timeout_seconds", 10)),
            ),
        ),
        image=ImageModerationConfig(
            backend=image_raw.get("backend", "cloud_vision"),
            gif_frame_sample_fps=int(image_raw.get("gif_frame_sample_fps", 1)),
            gif_max_frames=int(image_raw.get("gif_max_frames", 30)),
            fallback_on_api_error=image_raw.get("fallback_on_api_error", "review"),
        ),
        actions=ActionsConfig(
            reviewer_email=reviewer_email,
            reviewer_chat_user_id=reviewer_chat_user_id,
        ),
        pubsub=PubSubConfig(
            project_id=pubsub_project,
            subscription_id=pubsub_sub,
            topic_id=pubsub_topic,
            workspace_events_subscription_name=workspace_events_sub,
            workspace_events_target_resource=workspace_events_target,
        ),
        interaction_server=InteractionServerConfig(
            host=interaction_raw.get("host", "0.0.0.0"),
            port=int(interaction_raw.get("port", 8080)),
        ),
        _anthropic_api_key=anthropic_key,
        _service_account_key_path=sa_key_path,
    )
