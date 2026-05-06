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
    """202 Accepted response from POST /capture."""

    model_config = ConfigDict(json_encoders={datetime: lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")})

    capture_id: str
    doc_id: str
    web_url: str
    status: CaptureStatus
    platform: str
    initial_path: str
    created_at: datetime
