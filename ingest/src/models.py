"""Wire-level Pydantic models + URL normalization helpers."""

from __future__ import annotations

import enum
import hashlib
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


# ── Enums & helpers ──────────────────────────────────────────────────


class CaptureStatus(str, enum.Enum):
    """State machine values. See spec §5."""
    QUEUED = "queued"
    EXTRACTING = "extracting"
    CLASSIFYING = "classifying"
    FILING = "filing"
    DONE = "done"
    FAILED = "failed"
    DELETED = "deleted"


_UTM_PREFIXES = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid")


def normalized_url(url: str) -> str:
    """Canonicalize a URL for idempotency.

    - lowercases the host (path & query stay case-sensitive)
    - drops fragments
    - removes utm_* / fbclid / gclid / mc_cid / mc_eid query params
    - removes trailing slash on non-root paths
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    netloc = host.lower()
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"

    pairs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if not any(k.lower().startswith(p) for p in _UTM_PREFIXES)]
    query = urlencode(pairs)

    path = parsed.path
    if path.endswith("/") and len(path) > 1:
        path = path[:-1]

    return urlunparse((parsed.scheme.lower(), netloc, path, "", query, ""))


def url_hash(url: str) -> str:
    """Stable SHA-256 of the normalized URL for idempotency lookups."""
    return hashlib.sha256(normalized_url(url).encode("utf-8")).hexdigest()


# ── Wire models ──────────────────────────────────────────────────────


class CaptureRequest(BaseModel):
    """POST /capture body. At least one of url/shared_text must be present;
    enforced in the handler, not the model (Pydantic doesn't natively
    express "one-of-N" cleanly without extra ceremony)."""

    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    source_app: str | None = Field(default=None, max_length=128)
    shared_title: str | None = Field(default=None, max_length=512)
    shared_text: str | None = Field(default=None, max_length=10_000)

    @field_validator("url", mode="before")
    @classmethod
    def _validate_url_scheme(cls, value):
        if value is None:
            return None
        # Validate via HttpUrl to get scheme checking, then coerce to str.
        validated = HttpUrl(str(value))
        return str(validated)


class CaptureResponse(BaseModel):
    """202 Accepted response from POST /capture.

    Pydantic v2 serializes UTC-aware datetime as ISO-8601 with Z suffix
    natively when the value is timezone-aware — no encoder needed.
    """

    capture_id: str
    doc_id: str
    web_url: str
    status: CaptureStatus
    platform: str
    initial_path: str
    created_at: datetime


class CaptureItem(BaseModel):
    """Single row returned in lists.

    Phase 7 history view in the iOS app consumes this shape.
    """

    capture_id: str
    url: str | None = None
    platform: str
    status: CaptureStatus
    doc_id: str | None = None
    web_url: str | None = None
    topic_path: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class CapturesPage(BaseModel):
    """Paginated list response."""

    items: list[CaptureItem]
    next_cursor: str | None = None


class CaptureDetail(CaptureItem):
    """Single capture detail, with diagnostics for the iOS detail screen."""

    error: str | None = None
    retry_count: int = 0
    classifier_reasoning: str | None = None


# ── Templates (Phase 14) ─────────────────────────────────────────────


class ContentTemplateView(BaseModel):
    """API response shape for a template row."""

    id: str
    platform_id: str
    topic: str
    name: str
    system_prompt: str
    status: str
    generator_meta: dict | None = None
    created_by: str
    created_at: datetime
    updated_at: datetime
    usage_count: int = 0


class CreateTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform_id: str = Field(..., min_length=1, max_length=64)
    topic: str = Field(..., min_length=1, max_length=128)
    name: str = Field(..., min_length=1, max_length=128)
    system_prompt: str = Field(..., min_length=1)


class UpdateTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform_id: str | None = Field(default=None, min_length=1, max_length=64)
    topic: str | None = Field(default=None, min_length=1, max_length=128)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    system_prompt: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _at_least_one_field(self):
        if all(v is None for v in (self.platform_id, self.topic, self.name, self.system_prompt)):
            raise ValueError("At least one field must be provided.")
        return self


class SynthesizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform_id: str = Field(..., min_length=1, max_length=64)
    topic: str = Field(..., min_length=1, max_length=128)
    sample_capture_id: str | None = None
