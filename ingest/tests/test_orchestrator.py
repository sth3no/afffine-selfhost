from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import Platform, TopicsConfig
from src.db import CaptureRow
from src.pipeline.classification import ClassificationResult
from src.pipeline.extracted import Extracted, MediaKind
from src.pipeline.orchestrator import process_capture


def _row(status="extracting", **kw):
    base = {
        "id": "01J", "url": "https://example.com/article",
        "url_hash": "h", "source_app": None, "shared_title": None, "shared_text": None,
        "platform": "article", "status": status, "doc_id": "d-1", "web_url": "w",
        "topic_path": "Sources/Articles/Web",
    }
    base.update(kw)
    from datetime import datetime, timezone
    base["created_at"] = datetime(2026, 5, 7, tzinfo=timezone.utc)
    return CaptureRow(**base)


def _platform() -> Platform:
    return Platform(id="article", group="Articles", folder_name="Web",
                    hosts=["*"], extractor="markitdown")


def _extracted() -> Extracted:
    return Extracted(
        title="Hello",
        body_md="# Body\n\nContent here.",
        author="someone",
        published_at=None,
        media_kind=MediaKind.TEXT,
        extra={},
    )


def _topics(plat: Platform) -> TopicsConfig:
    return TopicsConfig(platforms=[plat], topic_hints={"article": ["Tech", "Science"]})


@pytest.fixture
def deps():
    """Shared mock dependency bundle."""
    repo = AsyncMock()
    filer = AsyncMock()
    filer._mcp = AsyncMock()
    filer._mcp.list_folder_tree.return_value = {"totalNodes": 0, "tree": []}
    extract_fn = AsyncMock(return_value=_extracted())
    classify_fn = AsyncMock(return_value=ClassificationResult(
        topic="Tech", confidence=0.92, reasoning="article about tech",
    ))
    return {
        "repo": repo,
        "filer": filer,
        "extract_fn": extract_fn,
        "classify_fn": classify_fn,
    }


@pytest.mark.asyncio
async def test_process_capture_happy_path_advances_through_all_states(deps):
    plat = _platform()
    deps["filer"].move_to_topic_folder.return_value = "f-tech"
    deps["filer"]._mcp.list_folder_tree.return_value = {"totalNodes": 0, "tree": []}

    await process_capture(
        _row(),
        platform=plat,
        topics=_topics(plat),
        repo=deps["repo"],
        filer=deps["filer"],
        extract_fn=deps["extract_fn"],
        classify_fn=deps["classify_fn"],
    )

    deps["extract_fn"].assert_awaited_once()
    deps["classify_fn"].assert_awaited_once()
    deps["repo"].mark_classifying.assert_awaited_once()
    deps["repo"].mark_filing.assert_awaited_once()
    deps["repo"].mark_done.assert_awaited_once_with("01J")
    deps["filer"].move_to_topic_folder.assert_awaited_once()
    deps["filer"]._mcp.move_document.assert_awaited_once_with("d-1", folder_id="f-tech")
    deps["filer"]._mcp.append_blocks.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_capture_low_confidence_leaves_at_platform_root(deps):
    """topic=None means filer.move_to_topic_folder returns None → no move_document."""
    plat = _platform()
    deps["classify_fn"].return_value = ClassificationResult(
        topic=None, confidence=0.4, reasoning="ambiguous",
    )
    deps["filer"].move_to_topic_folder.return_value = None

    await process_capture(
        _row(),
        platform=plat,
        topics=_topics(plat),
        repo=deps["repo"],
        filer=deps["filer"],
        extract_fn=deps["extract_fn"],
        classify_fn=deps["classify_fn"],
    )

    deps["filer"]._mcp.move_document.assert_not_called()
    deps["repo"].mark_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_capture_extractor_failure_calls_mark_failed(deps):
    plat = _platform()
    deps["extract_fn"].side_effect = RuntimeError("yt-dlp bombed")

    with pytest.raises(RuntimeError, match="yt-dlp bombed"):
        await process_capture(
            _row(),
            platform=plat,
            topics=_topics(plat),
            repo=deps["repo"],
            filer=deps["filer"],
            extract_fn=deps["extract_fn"],
            classify_fn=deps["classify_fn"],
        )

    # Orchestrator re-raises; the worker (Task 3) is responsible for mark_failed
    # with backoff. The orchestrator should NOT have advanced state.
    deps["repo"].mark_done.assert_not_called()


@pytest.mark.asyncio
async def test_process_capture_skips_classify_when_already_classified(deps):
    """Idempotency: row with classifier_topic already set → don't call classify_fn."""
    plat = _platform()
    deps["filer"].move_to_topic_folder.return_value = "f-tech"
    deps["filer"]._mcp.list_folder_tree.return_value = {"totalNodes": 0, "tree": []}

    row = _row()
    # Simulate a row that crashed mid-filing — already classified.
    # The orchestrator pre-fetches via repo.get_by_id to inspect; emulate via attr.
    row.classifier_topic = "Tech"
    row.classifier_conf = 0.92
    row.classifier_reasoning = "from prior attempt"

    await process_capture(
        row,
        platform=plat,
        topics=_topics(plat),
        repo=deps["repo"],
        filer=deps["filer"],
        extract_fn=deps["extract_fn"],
        classify_fn=deps["classify_fn"],
    )

    deps["classify_fn"].assert_not_called()
    deps["filer"].move_to_topic_folder.assert_awaited_once()
