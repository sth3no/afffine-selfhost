"""Async worker loop. Single task per service instance.

Pumps captures rows through the orchestrator. Polls DB every
POLL_INTERVAL_SEC seconds via claim_next_queued / claim_due_failed.
Failures trigger backoff scheduled in [60s, 5min, 30min] then permanent
(next_attempt_at = None after 3 retries; never picked up again via
claim_due_failed).

Crash recovery is the caller's responsibility (lifespan): call
repo.reset_in_flight_to_queued() BEFORE starting the worker task so any
in-flight rows from a prior process restart are picked up here.

Dependency injection: process_fn, platform_for, repo_factory are closures
supplied by api.py's lifespan. Tests inject AsyncMock for process_fn and
a simple lambda for repo_factory.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from src.config import Platform, TopicsConfig
from src.db import CaptureRepository, CaptureRow


log = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 2.0
BACKOFF_SCHEDULE_SEC = [60, 300, 1800]  # retry 1, 2, 3


def compute_next_attempt_at(*, retry_count: int, now: datetime) -> datetime | None:
    """Return when this row should be retried, or None for permanent failure.

    retry_count is 1-indexed: the value AFTER the current failure.
    retry_count > len(BACKOFF_SCHEDULE_SEC) → None (permanent fail).
    """
    idx = retry_count - 1
    if idx < 0 or idx >= len(BACKOFF_SCHEDULE_SEC):
        return None
    return now + timedelta(seconds=BACKOFF_SCHEDULE_SEC[idx])


ProcessFunc = Callable[..., Awaitable[None]]
PlatformLookup = Callable[[CaptureRow], Platform]
RepoFactory = Callable[[Any], CaptureRepository]


class Worker:
    """Single asyncio task that pumps the captures queue.

    Constructor args:
        pool: asyncpg pool (or duck-typed mock in tests)
        repo_factory: callable(conn) -> CaptureRepository; lets tests inject
            a single shared mock repo without needing a real pool.
        process_fn: async (row, *, platform, topics, repo) -> None;
            wraps the orchestrator in production, AsyncMock in tests.
        platform_for: callable(row) -> Platform; looks up the platform
            config for a row's platform id.
        topics: TopicsConfig; forwarded to process_fn.
        poll_interval_sec: idle sleep between polls (default 2.0s).
    """

    def __init__(
        self,
        *,
        pool: Any,
        repo_factory: RepoFactory,
        process_fn: ProcessFunc,
        platform_for: PlatformLookup,
        topics: TopicsConfig,
        poll_interval_sec: float = POLL_INTERVAL_SEC,
    ) -> None:
        self._pool = pool
        self._repo_factory = repo_factory
        self._process = process_fn
        self._platform_for = platform_for
        self._topics = topics
        self._poll = poll_interval_sec
        self._stop = asyncio.Event()
        self._alive = False

    @property
    def alive(self) -> bool:
        return self._alive

    def stop(self) -> None:
        """Signal the worker loop to exit after its current iteration."""
        self._stop.set()

    async def _loop(self) -> None:
        """Main worker loop. Runs until stop() is called."""
        self._alive = True
        try:
            while not self._stop.is_set():
                row = await self._claim_next()
                if row is None:
                    # Nothing to do — idle until next poll or stop.
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=self._poll)
                    except asyncio.TimeoutError:
                        pass
                    continue

                # Dispatch to the process function with the repo bound to the
                # same connection context. Note: the claim UPDATE was its own
                # transaction; the orchestrator's mark_* calls are separate
                # transactions (one per step) for per-step idempotency.
                from src.logging_setup import set_capture_id
                with set_capture_id(row.id):
                    try:
                        platform = self._platform_for(row)
                        async with self._pool.acquire() as conn:
                            repo = self._repo_factory(conn)
                            await self._process(
                                row,
                                platform=platform,
                                topics=self._topics,
                                repo=repo,
                            )
                    except Exception as exc:
                        await self._handle_failure(row, exc)
        finally:
            self._alive = False

    async def _claim_next(self) -> CaptureRow | None:
        """Acquire a connection, try queued then due-failed."""
        async with self._pool.acquire() as conn:
            repo = self._repo_factory(conn)
            row = await repo.claim_next_queued()
            if row is not None:
                return row
            return await repo.claim_due_failed()

    async def _handle_failure(self, row: CaptureRow, exc: Exception) -> None:
        """Record a failed row with backoff. Reads prior retry_count from row."""
        # retry_count on the row is the count BEFORE this failure.
        # After this failure it becomes row.retry_count + 1.
        new_retry_count = row.retry_count + 1
        next_attempt_at = compute_next_attempt_at(
            retry_count=new_retry_count,
            now=datetime.now(timezone.utc),
        )
        log.warning(
            "capture %s failed (attempt %d): %s — next_attempt_at=%s",
            row.id, new_retry_count, exc, next_attempt_at,
        )
        async with self._pool.acquire() as conn:
            repo = self._repo_factory(conn)
            await repo.mark_failed(
                capture_id=row.id,
                error=str(exc),
                retry_count=new_retry_count,
                next_attempt_at=next_attempt_at,
            )
