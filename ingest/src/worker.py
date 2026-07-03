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
CAPTURE_TIMEOUT_SEC = 1800.0  # hard ceiling per capture; overridable via Settings
MAX_ERROR_CHARS = 2000  # cap on the error string persisted to captures.error


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
        capture_timeout_sec: float = CAPTURE_TIMEOUT_SEC,
    ) -> None:
        self._pool = pool
        self._repo_factory = repo_factory
        self._process = process_fn
        self._platform_for = platform_for
        self._topics = topics
        self._poll = poll_interval_sec
        self._capture_timeout = capture_timeout_sec
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._running_loops = 0

    @property
    def alive(self) -> bool:
        return self._running_loops > 0

    def stop(self) -> None:
        """Signal all worker loops to exit after their current iteration."""
        self._stop.set()

    def wake(self) -> None:
        """Skip the idle poll delay — called by the API right after a row
        is inserted so pickup latency is ~0 instead of up to poll_interval."""
        self._wake.set()

    async def _idle(self) -> None:
        """Sleep until poll timeout, stop(), or wake() — whichever first."""
        self._wake.clear()
        stop_t = asyncio.ensure_future(self._stop.wait())
        wake_t = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait(
                {stop_t, wake_t},
                timeout=self._poll,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (stop_t, wake_t):
                t.cancel()

    async def _loop(self) -> None:
        """Main worker loop. Runs until stop() is called.

        Resilient by design: a failed claim (e.g. Postgres restarting) is
        logged and retried after the poll interval — it must never kill the
        loop, because nothing restarts it and /health would go red until the
        container is bounced.
        """
        self._running_loops += 1
        try:
            while not self._stop.is_set():
                try:
                    row = await self._claim_next()
                except Exception as exc:  # noqa: BLE001 — claim must not kill the loop
                    log.warning(
                        "claim failed (transient DB error?) — retrying in %.1fs: %s: %s",
                        self._poll, type(exc).__name__, exc,
                    )
                    await self._idle()
                    continue
                if row is None:
                    # Nothing to do — idle until next poll, stop, or wake.
                    await self._idle()
                    continue

                # Dispatch to the process function with the repo bound to the
                # same connection context. Note: the claim UPDATE was its own
                # transaction; the orchestrator's mark_* calls are separate
                # transactions (one per step) for per-step idempotency.
                # collect_usage installs the LLM usage collector every call
                # site records into; the summary is persisted in the finally
                # so failed/timed-out attempts still account their spend.
                from src.logging_setup import set_capture_id
                from src.llm_usage import collect_usage
                with set_capture_id(row.id), collect_usage() as usage:
                    try:
                        platform = self._platform_for(row)
                        async with self._pool.acquire() as conn:
                            repo = self._repo_factory(conn)
                            await asyncio.wait_for(
                                self._process(
                                    row,
                                    platform=platform,
                                    topics=self._topics,
                                    repo=repo,
                                ),
                                timeout=self._capture_timeout,
                            )
                    except asyncio.TimeoutError:
                        await self._handle_failure_safe(
                            row,
                            TimeoutError(
                                f"capture processing timed out after "
                                f"{int(self._capture_timeout)}s"
                            ),
                        )
                    except Exception as exc:
                        await self._handle_failure_safe(row, exc)
                    finally:
                        await self._persist_usage_safe(row, usage)
        finally:
            self._running_loops -= 1

    async def _claim_next(self) -> CaptureRow | None:
        """Acquire a connection, try queued then due-failed."""
        async with self._pool.acquire() as conn:
            repo = self._repo_factory(conn)
            row = await repo.claim_next_queued()
            if row is not None:
                return row
            return await repo.claim_due_failed()

    async def _persist_usage_safe(self, row: CaptureRow, usage: Any) -> None:
        """Persist the capture's aggregated LLM usage + emit one structured
        log line. Best-effort: accounting must never kill the loop or mask
        the capture's real outcome."""
        try:
            summary = usage.summary()
            if summary is None:
                return
            log.info("capture llm usage", extra={"cost_breakdown": summary})
            async with self._pool.acquire() as conn:
                repo = self._repo_factory(conn)
                await repo.save_cost_breakdown(capture_id=row.id, breakdown=summary)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "could not persist usage for capture %s: %s: %s",
                row.id, type(exc).__name__, exc,
            )

    async def _handle_failure_safe(self, row: CaptureRow, exc: Exception) -> None:
        """_handle_failure, but a failure to persist the failure (DB down at
        exactly the wrong moment) must not kill the loop. The row stays
        'extracting' and is re-queued by crash recovery on next startup."""
        try:
            await self._handle_failure(row, exc)
        except Exception as persist_exc:  # noqa: BLE001
            log.error(
                "could not persist failure for capture %s (row stays in-flight "
                "until next startup recovery): %s: %s",
                row.id, type(persist_exc).__name__, persist_exc,
            )

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
        # str(exc) can be empty (bare TimeoutError) or huge (httpx errors
        # embedding response bodies) — normalize both before persisting.
        error = (str(exc) or type(exc).__name__)[:MAX_ERROR_CHARS]
        async with self._pool.acquire() as conn:
            repo = self._repo_factory(conn)
            await repo.mark_failed(
                capture_id=row.id,
                error=error,
                retry_count=new_retry_count,
                next_attempt_at=next_attempt_at,
            )
