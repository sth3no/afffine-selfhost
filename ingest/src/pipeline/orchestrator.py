"""Per-row pipeline orchestrator.

State machine: extracting → classifying → filing → done.
Per-step idempotency: skips work whose result is already persisted on
the row (e.g., classifier_topic populated → don't re-classify).

Exceptions propagate to the worker, which calls repo.mark_failed with
the appropriate backoff. The orchestrator never calls mark_failed
itself — separation of concerns.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from src.config import Platform, TopicsConfig
from src.db import CaptureRepository, CaptureRow
from src.pipeline.classification import ClassificationResult
from src.pipeline.extracted import Extracted
from src.pipeline.filer import Filer

log = logging.getLogger(__name__)

ExtractFunc = Callable[[str, Platform], Awaitable[Extracted]]
ClassifyFunc = Callable[..., Awaitable[ClassificationResult]]  # kwargs-style


async def process_capture(
    row: CaptureRow,
    *,
    platform: Platform,
    topics: TopicsConfig,
    repo: CaptureRepository,
    filer: Filer,
    extract_fn: ExtractFunc,
    classify_fn: ClassifyFunc,
) -> None:
    """Run the full pipeline for one capture row.

    Pre-conditions: row.status == 'extracting' (already claimed by the worker).
    Post-conditions: row.status == 'done' (success), or exception propagated
    to caller (failure → caller responsible for mark_failed).
    """
    if not row.url:
        # Phase 3 supports text-only captures (shared_text). Phase 6's
        # extractors all expect URLs; text-only goes straight to classifying
        # using shared_text as the body.
        extracted = Extracted(
            title=row.shared_title,
            body_md=row.shared_text or "",
            author=None,
            published_at=None,
            media_kind=_media_kind_for_text(),
            extra={"text_only": True},
        )
    else:
        extracted = await extract_fn(row.url, platform)

    log.info("transition", extra={"step": "extracted", "platform": platform.id})

    # ── Classify (or reuse cached classifier output on retry) ────────
    if row.classifier_topic is not None or row.classifier_conf is not None:
        result = ClassificationResult(
            topic=row.classifier_topic,
            confidence=float(row.classifier_conf or 0.0),
            reasoning=row.classifier_reasoning or "(reused from prior attempt)",
        )
    else:
        sibling_topics = _list_existing_siblings(filer, platform)
        topic_hints = topics.topic_hints.get(platform.id, [])
        result = await classify_fn(
            extracted=extracted,
            platform=platform,
            sibling_topics=await sibling_topics,
            topic_hints=topic_hints,
        )
        await repo.mark_classifying(
            capture_id=row.id,
            topic=result.topic,
            confidence=result.confidence,
            reasoning=result.reasoning,
        )

    log.info("transition", extra={"step": "classified", "topic": result.topic, "confidence": result.confidence})

    # ── File (move + append body) ────────────────────────────────────
    platform_path = ["Sources", platform.group, platform.folder_name]
    folder_id = await filer.move_to_topic_folder(platform_path=platform_path, result=result)

    if folder_id is not None:
        topic_path = "/".join(platform_path + [result.topic or ""])
    else:
        topic_path = "/".join(platform_path)

    await repo.mark_filing(capture_id=row.id, topic_path=topic_path)

    log.info("transition", extra={"step": "filed", "topic_path": topic_path})

    if folder_id is not None:
        await filer._mcp.move_document(row.doc_id, folder_id=folder_id)

    # Replace stub doc body with extracted content.
    body_blocks = [
        {"type": "paragraph", "text": extracted.body_md or "(no extracted content)"},
    ]
    await filer._mcp.append_blocks(row.doc_id, body_blocks)

    # ── Done ─────────────────────────────────────────────────────────
    await repo.mark_done(row.id)
    log.info("transition", extra={"step": "done"})


async def _list_existing_siblings(filer: Filer, platform: Platform) -> list[str]:
    """Return immediate child folder names under Sources/<group>/<platform>/."""
    tree = await filer._mcp.list_folder_tree()
    siblings = tree.get("tree", [])
    for segment in ("Sources", platform.group, platform.folder_name):
        match = next((n for n in siblings if n.get("name") == segment), None)
        if match is None:
            return []
        siblings = match.get("children", []) or []
    return [s.get("name") for s in siblings if s.get("type") == "folder" and s.get("name")]


def _media_kind_for_text():
    from src.pipeline.extracted import MediaKind
    return MediaKind.TEXT
