from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment.

    Phase 1: PORT, DATABASE_URL, DB_ADMIN_URL, INGEST_API_TOKEN.
    Phase 3: affine_server_external_url, affine_workspace_id (used by api.py
    to build web_url for the iOS app).
    Later phases extend this — never delete fields, only add.
    """

    port: int = 3200
    database_url: str = "postgresql://placeholder@localhost/affine_ingest"
    db_admin_url: str | None = None
    ingest_api_token: str = "dev-token"
    affine_server_external_url: str = "http://localhost:3010"
    affine_workspace_id: str = ""
    version: str = "0.1.0"
    max_transcript_min: int = 30
    max_body_chars: int = 50_000
    cobalt_api_url: str = "http://cobalt:9000"
    cobalt_duration_limit: int = 10800
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    classifier_model: str = "claude-haiku-4-5-20251001"
    summarizer_model: str = "claude-haiku-4-5"
    summarizer_max_body_chars: int = 4000
    embedding_model: str = "text-embedding-3-small"
    confidence_floor: float = 0.6
    similarity_threshold: float = 0.85
    youtube_cookies_path: str = "/run/cookies/youtube.txt"
    # Phase 13 — video frame analysis
    video_analysis_enabled: bool = True
    vision_model: str = "claude-sonnet-4-6"
    cobalt_video_max_size_mb: int = 200
    max_frames_per_video: int = 12
    max_keyframes_in_doc: int = 6
    keyframe_importance_threshold: int = 4
    frame_long_edge_px: int = 1024
    scenedetect_threshold: float = 27.0

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)


settings = Settings()


# ── Topics config (loaded from topics.yaml) ───────────────────────────


class Platform(BaseModel):
    id: str
    group: str
    folder_name: str
    hosts: list[str]
    extractor: str


class ReorgConfig(BaseModel):
    default_threshold: int = 15
    overrides: dict[str, int] = Field(default_factory=dict)


class TopicsConfig(BaseModel):
    platforms: list[Platform]
    topic_hints: dict[str, list[str]] = Field(default_factory=dict)
    reorg: ReorgConfig = Field(default_factory=ReorgConfig)


_DEFAULT_TOPICS_PATH = Path(__file__).resolve().parent.parent / "topics.yaml"


def load_topics(path: Path | None = None) -> TopicsConfig:
    """Read topics.yaml. Validates platforms list is non-empty.

    Optional sections (topic_hints, reorg) default to empty/sentinels so the
    file can grow over phases without breaking older code.
    """
    p = path or _DEFAULT_TOPICS_PATH
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    config = TopicsConfig.model_validate(raw)
    if not config.platforms:
        raise ValueError("topics.yaml must declare at least one platform")
    return config
