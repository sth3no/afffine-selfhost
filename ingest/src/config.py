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
    # Sonnet 4.6 for the rendering pipeline: quality matters more than
    # cost since we now map-reduce long transcripts, and the lede /
    # structured-analysis output is the load-bearing artifact.
    summarizer_model: str = "claude-sonnet-4-6"
    summarizer_max_body_chars: int = 12000
    # Map-reduce thresholds for long transcripts. If extracted.body_md
    # exceeds chunked_render_threshold_chars, we split into chunks
    # of chunk_size_chars (with chunk_overlap_chars overlap), summarize
    # each chunk via a single Sonnet call, then reduce all chunk
    # summaries into the final TemplatedOutput via one more Sonnet call.
    # Cost: ~N+1 Sonnet calls for an N-chunk transcript.
    chunked_render_threshold_chars: int = 12000
    chunk_size_chars: int = 8000
    chunk_overlap_chars: int = 500
    # Maximum chunks we'll process per capture. Caps cost on extreme
    # outliers (e.g. a 3-hour podcast transcript). Beyond this we
    # truncate and emit a note in the rendered doc.
    max_chunks_per_capture: int = 16
    # Hard per-capture ceiling. A hung extraction (stalled stream, wedged
    # subprocess) otherwise blocks a worker slot forever. 30 min comfortably
    # covers the worst legitimate case (long podcast → chunked render).
    capture_timeout_sec: int = 1800
    # Number of concurrent worker loops pumping the captures queue. The DB
    # claim is FOR UPDATE SKIP LOCKED-safe; folder creation is serialized
    # in-process by the Filer's lock.
    worker_concurrency: int = 2
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
    # Phase 16 — Frame-quality pre-filter (runs BETWEEN scene detect and vision call).
    # Defaults chosen to be conservative: drop only frames that are
    # obviously useless. Tune via env vars if the filter is over- or
    # under-aggressive on a particular video corpus.
    frame_blackness_threshold: float = 20.0   # mean grayscale pixel value 0-255; below = "too dark"
    frame_dedup_hamming_distance: int = 5     # imagehash pHash distance 0-64; below-or-equal = "duplicate"
    frame_entropy_threshold: float = 4.0      # Shannon entropy of grayscale histogram, bits 0-8; below = "too uniform"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)


settings = Settings()


def build_web_url(doc_id: str) -> str:
    """Construct the AFFiNE workspace doc URL from settings.

    Returns a degraded (host-only) URL when AFFINE_WORKSPACE_ID is empty —
    the URL is non-functional without a workspace, but callers keep working
    so the operator can fix the missing env without dropped captures.
    """
    base = settings.affine_server_external_url.rstrip("/")
    workspace = settings.affine_workspace_id
    if not workspace:
        return f"{base}/{doc_id}"
    return f"{base}/workspace/{workspace}/{doc_id}"


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
