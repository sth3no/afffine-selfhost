from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.db import CaptureRepository, CaptureRow


def _row_dict(**overrides):
    base = {
        "id": "01J", "url": "https://x", "url_hash": "h", "source_app": None,
        "shared_title": None, "shared_text": None, "platform": "article",
        "status": "done", "doc_id": "d", "web_url": "w",
        "topic_path": "Sources/Articles/Web/Tech",
        "classifier_topic": "Tech", "classifier_conf": 0.9,
        "classifier_reasoning": "ok",
        "retry_count": 0,
        "created_at": datetime(2026, 5, 7, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_list_captures_default_limit_returns_newest_first():
    conn = AsyncMock()
    conn.fetch.return_value = [_row_dict(id="b"), _row_dict(id="a")]
    repo = CaptureRepository(conn)
    rows = await repo.list_captures(limit=50)
    assert [r.id for r in rows] == ["b", "a"]
    sql = conn.fetch.call_args.args[0]
    assert "ORDER BY created_at DESC" in sql
    assert "LIMIT" in sql


@pytest.mark.asyncio
async def test_list_captures_filters_by_status():
    conn = AsyncMock()
    conn.fetch.return_value = []
    repo = CaptureRepository(conn)
    await repo.list_captures(limit=10, status="failed")
    sql = conn.fetch.call_args.args[0]
    args = conn.fetch.call_args.args[1:]
    assert "status" in sql
    assert "failed" in args


@pytest.mark.asyncio
async def test_list_captures_filters_by_platform():
    conn = AsyncMock()
    conn.fetch.return_value = []
    repo = CaptureRepository(conn)
    await repo.list_captures(limit=10, platform="instagram")
    sql = conn.fetch.call_args.args[0]
    args = conn.fetch.call_args.args[1:]
    assert "platform" in sql
    assert "instagram" in args


@pytest.mark.asyncio
async def test_list_captures_combines_filters():
    conn = AsyncMock()
    conn.fetch.return_value = []
    repo = CaptureRepository(conn)
    await repo.list_captures(limit=10, status="done", platform="instagram")
    args = conn.fetch.call_args.args[1:]
    assert "done" in args
    assert "instagram" in args


@pytest.mark.asyncio
async def test_list_captures_cursor_filters_older_than_before():
    conn = AsyncMock()
    conn.fetch.return_value = []
    repo = CaptureRepository(conn)
    cursor = datetime(2026, 5, 1, tzinfo=timezone.utc)
    await repo.list_captures(limit=10, before=cursor)
    sql = conn.fetch.call_args.args[0]
    assert "created_at <" in sql.replace(" ", "<") or "created_at <" in sql
    args = conn.fetch.call_args.args[1:]
    assert cursor in args


@pytest.mark.asyncio
async def test_mark_for_retry_resets_classifier_and_returns_row():
    conn = AsyncMock()
    conn.fetchrow.return_value = _row_dict(status="queued", classifier_topic=None,
                                          classifier_conf=None, classifier_reasoning=None,
                                          retry_count=0)
    repo = CaptureRepository(conn)
    row = await repo.mark_for_retry("01J")
    assert row is not None
    assert row.status == "queued"
    assert row.classifier_topic is None
    sql = conn.fetchrow.call_args.args[0]
    # Verify all the resets are in the SQL.
    for token in ("classifier_topic = NULL", "classifier_conf = NULL",
                  "classifier_reasoning = NULL", "error = NULL",
                  "retry_count = 0", "next_attempt_at = NULL", "status = 'queued'"):
        assert token in sql.replace("  ", " "), f"missing: {token}"


@pytest.mark.asyncio
async def test_mark_for_retry_returns_none_when_not_found():
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    repo = CaptureRepository(conn)
    assert await repo.mark_for_retry("missing") is None


@pytest.mark.asyncio
async def test_mark_deleted_returns_row_with_doc_id():
    conn = AsyncMock()
    conn.fetchrow.return_value = _row_dict(status="deleted")
    repo = CaptureRepository(conn)
    row = await repo.mark_deleted("01J")
    assert row is not None
    assert row.status == "deleted"
    assert row.doc_id == "d"
    sql = conn.fetchrow.call_args.args[0]
    assert "status = 'deleted'" in sql or "status='deleted'" in sql.replace(" ", "")
