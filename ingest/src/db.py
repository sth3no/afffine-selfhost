"""Captures repository (asyncpg). Phase 3 only needs three queries."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import asyncpg


@dataclass
class CaptureRow:
    id: str
    url: str | None
    url_hash: str
    source_app: str | None
    shared_title: str | None
    shared_text: str | None
    platform: str
    status: str
    doc_id: str | None
    web_url: str | None
    topic_path: str | None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


_INSERT_SQL = """
    INSERT INTO captures
        (id, url, url_hash, source_app, shared_title, shared_text,
         platform, status, doc_id, web_url, topic_path)
    VALUES
        ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
"""

_BASE_SELECT = """
    SELECT id, url, url_hash, source_app, shared_title, shared_text,
           platform, status, doc_id, web_url, topic_path, created_at
    FROM captures
"""


class CaptureRepository:
    """Thin wrapper around an asyncpg.Connection (or pool — duck-typed).

    Phase 3 callers pass a single Connection. Phase 6 will wire a pool
    with `async with pool.acquire() as conn` per request.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def insert(self, row: CaptureRow) -> None:
        await self._conn.execute(
            _INSERT_SQL,
            row.id,
            row.url,
            row.url_hash,
            row.source_app,
            row.shared_title,
            row.shared_text,
            row.platform,
            row.status,
            row.doc_id,
            row.web_url,
            row.topic_path,
        )

    async def get_by_url_hash(self, url_hash: str) -> CaptureRow | None:
        record = await self._conn.fetchrow(_BASE_SELECT + " WHERE url_hash = $1", url_hash)
        if record is None:
            return None
        return CaptureRow(**dict(record))

    async def get_by_id(self, capture_id: str) -> CaptureRow | None:
        record = await self._conn.fetchrow(_BASE_SELECT + " WHERE id = $1", capture_id)
        if record is None:
            return None
        return CaptureRow(**dict(record))


# ── Pool helpers ──────────────────────────────────────────────────────


def build_pool_kwargs(dsn: str, *, min_size: int = 1, max_size: int = 8) -> dict:
    """Return kwargs for `asyncpg.create_pool(...)`."""
    return {"dsn": dsn, "min_size": min_size, "max_size": max_size}


async def create_pool(dsn: str) -> asyncpg.Pool:
    """Create the asyncpg pool. Lifespan-managed by FastAPI in api.py."""
    return await asyncpg.create_pool(**build_pool_kwargs(dsn))
