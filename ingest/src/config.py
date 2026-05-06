from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment.

    Phase 1 only needs PORT, DATABASE_URL, DB_ADMIN_URL, INGEST_API_TOKEN.
    Later phases extend this — never delete fields, only add.
    """

    port: int = 3200
    database_url: str = "postgresql://placeholder@localhost/affine_ingest"
    db_admin_url: str | None = None
    ingest_api_token: str = "dev-token"
    version: str = "0.1.0"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)


settings = Settings()


from pathlib import Path

import yaml
from pydantic import BaseModel, Field


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
