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
    # Which scene-detection engine to drive the cut list with.
    #   "adaptive"     — PySceneDetect `AdaptiveDetector`. Rolling-window
    #                    comparison; handles slow dissolves, gentle camera
    #                    moves, and gradual lighting changes well. Default.
    #   "content"      — PySceneDetect `ContentDetector`. Classic single
    #                    threshold; over-cuts pans/dissolves.
    #   "ffmpeg_scdet" — ffmpeg's native `scdet` video filter. Several times
    #                    faster than PySceneDetect because it reuses the
    #                    decoder without going through OpenCV; less accurate
    #                    on slow dissolves but plenty good for fast-cut
    #                    edited content. Recommended once the operator has
    #                    measured that PySceneDetect is the bottleneck.
    scenedetect_algorithm: str = "adaptive"
    # ffmpeg_scdet-only: the `threshold` argument passed to ffmpeg's scdet
    # filter. ffmpeg's scale is 0-100 (mean absolute frame difference); 10
    # is the upstream default. Higher = fewer cuts.
    scenedetect_ffmpeg_threshold: float = 10.0
    # AdaptiveDetector-only: how much the rolling content-value average must
    # exceed the window mean to trigger a cut. Higher = fewer cuts. The
    # PySceneDetect default of 3.0 is a good general baseline.
    scenedetect_adaptive_threshold: float = 3.0
    # Minimum scene length in FRAMES. Suppresses back-to-back micro-cuts in
    # montages so a 12-frame intro animation isn't allowed to consume the
    # entire `max_frames_per_video` budget. 15 ≈ 0.5s @ 30fps.
    scenedetect_min_scene_len: int = 15
    # When True, the detector compares the luma (Y) channel only — ignoring
    # chroma. Faster, and shrugs off pure color-grading shifts that don't
    # really constitute scene cuts.
    scenedetect_luma_only: bool = True
    # Width (seconds) of the window around mid-scene over which ffmpeg's
    # `thumbnail` filter picks the most representative frame. 0 disables
    # the filter and falls back to a single-frame seek at the midpoint
    # (legacy behavior). 1.0–2.0s is the sweet spot for cleaner keyframes
    # at modest extra decode cost.
    frame_thumbnail_window_seconds: float = 1.0
    # Maximum number of ffmpeg extract subprocesses to run in parallel
    # per video. Each subprocess.run() releases the GIL during the child's
    # I/O wait, so a ThreadPoolExecutor here gives a real speedup. Capped
    # to avoid flooding small hosts; effective parallelism is also bounded
    # by the actual frame count.
    frame_extract_workers: int = 4
    # ffmpeg `-hwaccel` value passed to extract calls. Empty string (default)
    # disables hwaccel — safest for portable deploys. Set to "auto", "vaapi",
    # "cuda", "videotoolbox", etc. for hardware decode on hosts that support
    # it. Wrong values just cause ffmpeg to fall back to software decode with
    # a stderr warning, but mis-tuned hwaccel can be SLOWER than CPU decode
    # on very short clips — measure before enabling.
    ffmpeg_hwaccel: str = ""
    # Audio-based cut detection: when enabled, ffmpeg's `silencedetect` filter
    # is run alongside PySceneDetect and its silence-end timestamps are merged
    # into the keyframe candidate list. Useful for content where visual cuts
    # are sparse but speech has clear topic breaks (long screencasts, lectures,
    # talking-head podcasts). Off by default — adds one ffmpeg pass per video.
    frame_silence_cuts_enabled: bool = False
    # silencedetect `noise` threshold in dB (negative). Below this RMS, the
    # filter considers audio silent. -30 is a reasonable default for clean
    # speech recordings; lower (e.g. -40) for noisier sources.
    frame_silence_threshold_db: float = -30.0
    # Minimum silence duration (seconds) that counts as a topic break.
    # Conversational micro-pauses are usually < 1s; 1.5s skips those while
    # still catching real transitions.
    frame_silence_min_duration: float = 1.5
    # When merging silence-cut timestamps with scene-detect timestamps, drop
    # any candidate that's within this many seconds of an already-accepted
    # one (the earlier-source candidate wins). Prevents two near-identical
    # keyframes from blowing the vision-call budget.
    frame_candidate_dedup_seconds: float = 2.0
    # Phase 16 — Frame-quality pre-filter (runs BETWEEN scene detect and vision call).
    # Defaults chosen to be conservative: drop only frames that are
    # obviously useless. Tune via env vars if the filter is over- or
    # under-aggressive on a particular video corpus.
    frame_blackness_threshold: float = 20.0   # mean grayscale pixel value 0-255; below = "too dark"
    frame_dedup_hamming_distance: int = 5     # imagehash pHash distance 0-64; below-or-equal = "duplicate"
    frame_entropy_threshold: float = 4.0      # Shannon entropy of grayscale histogram, bits 0-8; below = "too uniform"

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
