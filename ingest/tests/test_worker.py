import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import Platform, TopicsConfig
from src.db import CaptureRow
from src.worker import BACKOFF_SCHEDULE_SEC, Worker, compute_next_attempt_at


def _make_pool():
    """Return a MagicMock pool whose acquire() supports 'async with pool.acquire() as conn'.

    asyncpg's pool.acquire() is a synchronous call that returns an async context
    manager (PoolConnectionContext). We mirror that pattern: pool is a MagicMock so
    pool.acquire() returns pool.acquire.return_value (a MagicMock) with __aenter__
    and __aexit__ properly set up.
    """
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


def _exhausted_returns_none(seq):
    """Return a side_effect callable that yields from seq then always returns None.

    AsyncMock.side_effect as a list raises StopAsyncIteration when exhausted
    (Python's PEP 479 coerces StopIteration to RuntimeError in coroutines).
    Using a generator avoids this — once the sequence is consumed, subsequent
    calls return None (meaning 'nothing to do').
    """
    items = list(seq)
    idx = [0]

    async def _fn(*args, **kwargs):
        if idx[0] < len(items):
            val = items[idx[0]]
            idx[0] += 1
            return val
        return None

    return _fn


def _row(retry_count=0):
    return CaptureRow(
        id="01J-w", url="https://example.com", url_hash="h",
        source_app=None, shared_title=None, shared_text=None,
        platform="article", status="extracting", doc_id="d", web_url="w",
        topic_path="Sources/Articles/Web",
        created_at=datetime(2026, 5, 7, tzinfo=timezone.utc),
        retry_count=retry_count,
    )


def test_backoff_schedule_is_60s_5min_30min():
    assert BACKOFF_SCHEDULE_SEC == [60, 300, 1800]


def test_compute_next_attempt_at_first_failure():
    now = datetime(2026, 5, 7, 12, tzinfo=timezone.utc)
    nxt = compute_next_attempt_at(retry_count=1, now=now)
    assert nxt == now + timedelta(seconds=60)


def test_compute_next_attempt_at_third_failure_schedules_30min():
    now = datetime(2026, 5, 7, 12, tzinfo=timezone.utc)
    nxt = compute_next_attempt_at(retry_count=3, now=now)
    assert nxt == now + timedelta(seconds=1800)


def test_compute_next_attempt_at_fourth_failure_returns_none_permanent():
    nxt = compute_next_attempt_at(retry_count=4, now=datetime.now(timezone.utc))
    assert nxt is None


@pytest.mark.asyncio
async def test_worker_processes_one_row_then_idles():
    repo = AsyncMock()
    repo.claim_next_queued.side_effect = _exhausted_returns_none([_row()])
    repo.claim_due_failed.return_value = None

    process_fn = AsyncMock()
    pool = _make_pool()

    w = Worker(
        pool=pool,
        repo_factory=lambda conn: repo,
        process_fn=process_fn,
        platform_for=lambda row: Platform(
            id="article", group="Articles", folder_name="Web",
            hosts=["*"], extractor="markitdown"
        ),
        topics=TopicsConfig(
            platforms=[Platform(
                id="article", group="Articles", folder_name="Web",
                hosts=["*"], extractor="markitdown"
            )]
        ),
        poll_interval_sec=0.01,
    )
    task = asyncio.create_task(w._loop())
    await asyncio.sleep(0.05)
    w.stop()
    await task

    process_fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_handles_failure_with_backoff():
    repo = AsyncMock()
    repo.claim_next_queued.side_effect = _exhausted_returns_none([_row(retry_count=0)])
    repo.claim_due_failed.return_value = None

    process_fn = AsyncMock(side_effect=RuntimeError("first failure"))
    pool = _make_pool()

    w = Worker(
        pool=pool,
        repo_factory=lambda conn: repo,
        process_fn=process_fn,
        platform_for=lambda row: Platform(
            id="article", group="Articles", folder_name="Web",
            hosts=["*"], extractor="markitdown"
        ),
        topics=TopicsConfig(
            platforms=[Platform(
                id="article", group="Articles", folder_name="Web",
                hosts=["*"], extractor="markitdown"
            )]
        ),
        poll_interval_sec=0.01,
    )
    task = asyncio.create_task(w._loop())
    await asyncio.sleep(0.05)
    w.stop()
    await task

    repo.mark_failed.assert_awaited_once()
    kwargs = repo.mark_failed.call_args.kwargs
    assert kwargs["error"]
    assert "first failure" in kwargs["error"]
    assert kwargs["retry_count"] == 1
    assert kwargs["next_attempt_at"] is not None  # not permanent yet


@pytest.mark.asyncio
async def test_worker_third_failure_marks_permanent():
    repo = AsyncMock()
    repo.claim_next_queued.side_effect = _exhausted_returns_none([_row(retry_count=3)])
    repo.claim_due_failed.return_value = None

    process_fn = AsyncMock(side_effect=RuntimeError("third failure"))
    pool = _make_pool()

    w = Worker(
        pool=pool,
        repo_factory=lambda conn: repo,
        process_fn=process_fn,
        platform_for=lambda row: Platform(
            id="article", group="Articles", folder_name="Web",
            hosts=["*"], extractor="markitdown"
        ),
        topics=TopicsConfig(
            platforms=[Platform(
                id="article", group="Articles", folder_name="Web",
                hosts=["*"], extractor="markitdown"
            )]
        ),
        poll_interval_sec=0.01,
    )
    task = asyncio.create_task(w._loop())
    await asyncio.sleep(0.05)
    w.stop()
    await task

    kwargs = repo.mark_failed.call_args.kwargs
    assert kwargs["next_attempt_at"] is None  # permanent
