from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.db import CaptureRow, CaptureRepository, build_pool_kwargs


@pytest.mark.asyncio
async def test_insert_capture_executes_correct_sql():
    conn = AsyncMock()
    conn.execute.return_value = "INSERT 0 1"

    repo = CaptureRepository(conn)
    row = CaptureRow(
        id="01J9X4M5",
        url="https://example.com",
        url_hash="abc123",
        source_app="Safari",
        shared_title="Hello",
        shared_text=None,
        platform="article",
        status="queued",
        doc_id="d-1",
        web_url="https://affine.example.com/.../d-1",
        topic_path="Sources/Articles/Web",
    )
    await repo.insert(row)

    sql, *args = conn.execute.call_args.args
    assert "INSERT INTO captures" in sql
    # Verify all required columns are bound in order.
    assert args[0] == "01J9X4M5"
    assert args[1] == "https://example.com"
    assert args[2] == "abc123"
    # 11+ args total; spot-check that bind count is sane (no SQL injection
    # via missing $N).
    assert sql.count("$") >= 11


@pytest.mark.asyncio
async def test_get_by_url_hash_returns_row_when_present():
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": "01J9X4M5",
        "url": "https://example.com",
        "url_hash": "abc",
        "source_app": None,
        "shared_title": None,
        "shared_text": None,
        "platform": "article",
        "status": "queued",
        "doc_id": "d-1",
        "web_url": "...",
        "topic_path": "Sources/Articles/Web",
        "created_at": datetime(2026, 5, 7, tzinfo=timezone.utc),
    }
    repo = CaptureRepository(conn)
    row = await repo.get_by_url_hash("abc")
    assert row is not None
    assert row.id == "01J9X4M5"


@pytest.mark.asyncio
async def test_get_by_url_hash_returns_none_when_absent():
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    repo = CaptureRepository(conn)
    assert await repo.get_by_url_hash("nope") is None


@pytest.mark.asyncio
async def test_get_by_id_returns_row():
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": "01J9X4M5",
        "url": "x",
        "url_hash": "y",
        "source_app": None,
        "shared_title": None,
        "shared_text": None,
        "platform": "article",
        "status": "queued",
        "doc_id": "d",
        "web_url": "...",
        "topic_path": "...",
        "created_at": datetime(2026, 5, 7, tzinfo=timezone.utc),
    }
    repo = CaptureRepository(conn)
    row = await repo.get_by_id("01J9X4M5")
    assert row.id == "01J9X4M5"


def test_build_pool_kwargs_parses_url():
    kwargs = build_pool_kwargs("postgresql://user:pass@host:5432/db")
    assert kwargs["dsn"] == "postgresql://user:pass@host:5432/db"
    assert kwargs["min_size"] == 1
    assert kwargs["max_size"] >= 4
