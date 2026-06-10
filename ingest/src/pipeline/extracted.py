"""Normalized output of every extractor.

Phase 4 produces this; Phase 5 (classifier) consumes it; Phase 6 (worker)
threads it through the pipeline state machine.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class MediaKind(str, enum.Enum):
    TEXT = "text"
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    MIXED = "mixed"


@dataclass
class Extracted:
    """Normalized extraction result. Optional fields are None when the
    underlying source didn't provide them.

    Fields:
        title:        page title / video title / first heading; None when
                      the source had no obvious title.
        body_md:      cleaned Markdown text. The classifier reads this.
                      Capped at MAX_BODY_CHARS by truncate_body().
        author:       channel name / author / submitter when known.
        published_at: original publication timestamp when known.
        media_kind:   coarse content type (drives prompt structure in
                      Phase 5: video transcripts get different treatment
                      than article prose).
        extra:        platform-specific extras (channel id, hashtags, sub,
                      duration_seconds, ...). Free-form dict; the classifier
                      may ignore it.
    """

    title: str | None
    body_md: str
    author: str | None
    published_at: datetime | None
    media_kind: MediaKind
    extra: dict[str, Any] = field(default_factory=dict)


def truncate_body(body: str, *, limit: int) -> str:
    """Cap a markdown body at `limit` chars, appending `[...truncated]`."""
    if len(body) <= limit:
        return body
    return body[:limit] + "\n\n[...truncated]"


def to_snapshot(extracted: Extracted) -> dict[str, Any]:
    """Serialize an Extracted record to a JSON-able dict for the
    captures.extracted_snapshot column.

    `url` is intentionally omitted — it lives on the parent capture row.
    """
    return {
        "title": extracted.title,
        "body_md": extracted.body_md,
        "author": extracted.author,
        "published_at": extracted.published_at.isoformat() if extracted.published_at else None,
        "media_kind": extracted.media_kind.value,
        "extra": extracted.extra,
    }


def from_snapshot(snap: dict[str, Any] | str) -> Extracted:
    """Deserialize an extracted_snapshot (dict or JSON string, depending on
    the asyncpg codec in play) back into an Extracted record."""
    if isinstance(snap, str):
        import json
        snap = json.loads(snap)
    published_at = snap.get("published_at")
    return Extracted(
        title=snap.get("title"),
        body_md=snap.get("body_md", ""),
        author=snap.get("author"),
        published_at=datetime.fromisoformat(published_at) if published_at else None,
        media_kind=MediaKind(snap.get("media_kind", "video")),
        extra=snap.get("extra") or {},
    )
