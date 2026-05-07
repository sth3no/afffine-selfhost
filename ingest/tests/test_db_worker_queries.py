from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from src.db import CaptureRepository


def _row_dict(**overrides):
    base = {
        "id": "01J", "url": "https://x", "url_hash": "h", "source_app": None,
        "shared_title": None, "shared_text": None, "platform": "article",
        "status": "queued", "doc_id": "d", "web_url": "w",
        "topic_path": "Sources/Articles/Web",
        "created_at": datetime(2026, 5, 7, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_claim_next_queued_returns_row_and_updates_status():
    conn = AsyncMock()
    conn.fetchrow.return_value = _row_dict(status="extracting")
    repo = CaptureRepository(conn)
    row = await repo.claim_next_queued()
    assert row is not None
    assert row.status == "extracting"
    sql = conn.fetchrow.call_args.args[0]
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "status='extracting'" in sql or "status = 'extracting'" in sql.replace(" ", "")
    assert "WHERE status='queued'" in sql or "WHERE status = 'queued'" in sql.replace(" ", "")


@pytest.mark.asyncio
async def test_claim_next_queued_returns_none_when_no_rows():
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    repo = CaptureRepository(conn)
    assert await repo.claim_next_queued() is None


@pytest.mark.asyncio
async def test_claim_due_failed_filters_on_next_attempt_at():
    conn = AsyncMock()
    conn.fetchrow.return_value = _row_dict(status="extracting")
    repo = CaptureRepository(conn)
    await repo.claim_due_failed()
    sql = conn.fetchrow.call_args.args[0]
    assert "next_attempt_at" in sql
    assert "<= NOW()" in sql.replace(" ", "<=NOW()") or "<= now()" in sql.lower()


@pytest.mark.asyncio
async def test_mark_classifying_binds_all_fields():
    conn = AsyncMock()
    repo = CaptureRepository(conn)
    await repo.mark_classifying(
        capture_id="01J",
        topic="Recipes",
        confidence=0.92,
        reasoning="dish photo",
    )
    sql, *args = conn.execute.call_args.args
    assert "status='classifying'" in sql.replace(" ", "") or "status = 'classifying'" in sql
    assert args == ["01J", "Recipes", 0.92, "dish photo"]


@pytest.mark.asyncio
async def test_mark_filing_persists_topic_path():
    conn = AsyncMock()
    repo = CaptureRepository(conn)
    await repo.mark_filing(capture_id="01J", topic_path="Sources/Socials/Instagram/Recipes")
    sql, *args = conn.execute.call_args.args
    assert "topic_path" in sql
    assert args == ["01J", "Sources/Socials/Instagram/Recipes"]


@pytest.mark.asyncio
async def test_mark_done_sets_terminal_status():
    conn = AsyncMock()
    repo = CaptureRepository(conn)
    await repo.mark_done("01J")
    sql, *args = conn.execute.call_args.args
    assert "status='done'" in sql.replace(" ", "") or "status = 'done'" in sql
    assert "completed_at" in sql
    assert args == ["01J"]


@pytest.mark.asyncio
async def test_mark_failed_schedules_retry():
    conn = AsyncMock()
    repo = CaptureRepository(conn)
    next_at = datetime(2026, 5, 7, 12, tzinfo=timezone.utc)
    await repo.mark_failed(
        capture_id="01J",
        error="boom",
        retry_count=1,
        next_attempt_at=next_at,
    )
    sql, *args = conn.execute.call_args.args
    assert "status='failed'" in sql.replace(" ", "") or "status = 'failed'" in sql
    assert args == ["01J", "boom", 1, next_at]


@pytest.mark.asyncio
async def test_count_active_returns_int():
    conn = AsyncMock()
    conn.fetchval.return_value = 7
    repo = CaptureRepository(conn)
    assert await repo.count_active() == 7
    sql = conn.fetchval.call_args.args[0]
    assert "count(*)" in sql.lower()
    assert "queued" in sql
    assert "failed" in sql


@pytest.mark.asyncio
async def test_reset_in_flight_to_queued_returns_count():
    conn = AsyncMock()
    conn.fetch.return_value = [{"id": "01J-a"}, {"id": "01J-b"}]  # two rows reset
    repo = CaptureRepository(conn)
    n = await repo.reset_in_flight_to_queued()
    assert n == 2
    sql = conn.fetch.call_args.args[0]
    assert "extracting" in sql
    assert "classifying" in sql
    assert "filing" in sql
    assert "queued" in sql
