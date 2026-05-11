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
    classifier_topic: str | None = None
    classifier_conf: float | None = None
    classifier_reasoning: str | None = None
    retry_count: int = 0
    extracted_snapshot: Any | None = None


_INSERT_SQL = """
    INSERT INTO captures
        (id, url, url_hash, source_app, shared_title, shared_text,
         platform, status, doc_id, web_url, topic_path)
    VALUES
        ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
"""

_BASE_SELECT = """
    SELECT id, url, url_hash, source_app, shared_title, shared_text,
           platform, status, doc_id, web_url, topic_path,
           classifier_topic, classifier_conf, classifier_reasoning,
           retry_count, created_at, extracted_snapshot
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

    # ── Worker lifecycle queries (Phase 6) ───────────────────────────

    async def claim_next_queued(self) -> CaptureRow | None:
        """Atomically claim the oldest queued row, transitioning to 'extracting'."""
        sql = """
            UPDATE captures SET status='extracting', updated_at=NOW()
            WHERE id = (
                SELECT id FROM captures
                WHERE status='queued'
                ORDER BY created_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, url, url_hash, source_app, shared_title, shared_text,
                      platform, status, doc_id, web_url, topic_path,
                      classifier_topic, classifier_conf, classifier_reasoning,
                      retry_count, created_at, extracted_snapshot
        """
        rec = await self._conn.fetchrow(sql)
        return None if rec is None else CaptureRow(**dict(rec))

    async def claim_due_failed(self) -> CaptureRow | None:
        """Atomically claim the oldest failed row whose retry window has opened."""
        sql = """
            UPDATE captures SET status = 'extracting', updated_at = NOW()
            WHERE id = (
                SELECT id FROM captures
                WHERE status = 'failed' AND next_attempt_at <= NOW()
                ORDER BY next_attempt_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, url, url_hash, source_app, shared_title, shared_text,
                      platform, status, doc_id, web_url, topic_path,
                      classifier_topic, classifier_conf, classifier_reasoning,
                      retry_count, created_at, extracted_snapshot
        """
        rec = await self._conn.fetchrow(sql)
        return None if rec is None else CaptureRow(**dict(rec))

    async def mark_classifying(
        self, *, capture_id: str, topic: str | None, confidence: float, reasoning: str
    ) -> None:
        await self._conn.execute(
            """
            UPDATE captures
            SET status = 'classifying',
                classifier_topic = $2,
                classifier_conf = $3,
                classifier_reasoning = $4,
                updated_at = NOW()
            WHERE id = $1
            """,
            capture_id, topic, confidence, reasoning,
        )

    async def mark_filing(self, *, capture_id: str, topic_path: str) -> None:
        await self._conn.execute(
            """
            UPDATE captures
            SET status = 'filing', topic_path = $2, updated_at = NOW()
            WHERE id = $1
            """,
            capture_id, topic_path,
        )

    async def mark_done(self, capture_id: str) -> None:
        await self._conn.execute(
            """
            UPDATE captures
            SET status = 'done', completed_at = NOW(), updated_at = NOW()
            WHERE id = $1
            """,
            capture_id,
        )

    async def save_template_run(
        self,
        *,
        capture_id: str,
        template_id: str,
        prompt_used: str,
        output_raw: str,
    ) -> None:
        """Persist which template ran for this capture, plus the prompt and body
        snapshots needed for audit and replay."""
        await self._conn.execute(
            """
            UPDATE captures
            SET template_id = $2,
                template_prompt_used = $3,
                template_output_raw = $4,
                updated_at = NOW()
            WHERE id = $1
            """,
            capture_id, template_id, prompt_used, output_raw,
        )

    async def save_extracted_snapshot(
        self,
        *,
        capture_id: str,
        snapshot: dict,
    ) -> None:
        """Persist the Extracted record as JSONB so /captures/{id}/rerender
        can replay against the same inputs without re-fetching the source.

        The snapshot's `url` field is intentionally absent — url lives on the
        capture row, not on the Extracted record.

        JSONB encoding: asyncpg's default JSONB codec is text-format, so we
        json.dumps + $::jsonb cast manually. Matches the pattern in
        templates.py:insert_if_absent. Custom codec registration could be
        added in db.create_pool() to make this transparent, but for two
        write sites it's not worth the indirection yet.
        """
        import json
        await self._conn.execute(
            """
            UPDATE captures
            SET extracted_snapshot = $2::jsonb, updated_at = NOW()
            WHERE id = $1
            """,
            capture_id, json.dumps(snapshot),
        )

    async def mark_failed(
        self,
        *,
        capture_id: str,
        error: str,
        retry_count: int,
        next_attempt_at: datetime | None,
    ) -> None:
        await self._conn.execute(
            """
            UPDATE captures
            SET status = 'failed',
                error = $2,
                retry_count = $3,
                next_attempt_at = $4,
                updated_at = NOW()
            WHERE id = $1
            """,
            capture_id, error, retry_count, next_attempt_at,
        )

    async def count_active(self) -> int:
        return int(await self._conn.fetchval(
            """
            SELECT count(*) FROM captures
            WHERE status IN ('queued','extracting','classifying','filing','failed')
            """
        ))

    async def reset_in_flight_to_queued(self) -> int:
        """Crash recovery: rows mid-pipeline at startup go back to 'queued'."""
        rows = await self._conn.fetch(
            """
            UPDATE captures
            SET status = 'queued', updated_at = NOW()
            WHERE status IN ('extracting','classifying','filing')
            RETURNING id
            """
        )
        return len(rows)

    # ── Read + manage queries (Phase 7) ───────────────────────────────

    async def list_captures(
        self,
        *,
        limit: int,
        status: str | None = None,
        platform: str | None = None,
        before: "datetime | None" = None,
    ) -> "list[CaptureRow]":
        """List captures newest-first, optionally filtered by status/platform.

        `before` is a cursor — only rows with `created_at < before` are
        returned. Combine with limit for pagination.
        """
        clauses: list[str] = []
        args: list[Any] = []
        if status is not None:
            args.append(status)
            clauses.append(f"status = ${len(args)}")
        if platform is not None:
            args.append(platform)
            clauses.append(f"platform = ${len(args)}")
        if before is not None:
            args.append(before)
            clauses.append(f"created_at < ${len(args)}")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        args.append(limit)
        sql = f"""
            SELECT id, url, url_hash, source_app, shared_title, shared_text,
                   platform, status, doc_id, web_url, topic_path,
                   classifier_topic, classifier_conf, classifier_reasoning,
                   retry_count, created_at, extracted_snapshot
            FROM captures
            {where}
            ORDER BY created_at DESC
            LIMIT ${len(args)}
        """
        records = await self._conn.fetch(sql, *args)
        return [CaptureRow(**dict(r)) for r in records]

    async def mark_for_retry(self, capture_id: str) -> "CaptureRow | None":
        """Reset the row for re-processing by the worker.

        Clears classifier output + error + retry_count + next_attempt_at, and
        sets status back to 'queued'. Soft-deleted rows are NOT retried — the
        WHERE clause excludes them.
        """
        sql = """
            UPDATE captures
            SET status = 'queued',
                retry_count = 0,
                next_attempt_at = NULL,
                classifier_topic = NULL,
                classifier_conf = NULL,
                classifier_reasoning = NULL,
                error = NULL,
                updated_at = NOW()
            WHERE id = $1 AND status <> 'deleted'
            RETURNING id, url, url_hash, source_app, shared_title, shared_text,
                      platform, status, doc_id, web_url, topic_path,
                      classifier_topic, classifier_conf, classifier_reasoning,
                      retry_count, created_at, extracted_snapshot
        """
        rec = await self._conn.fetchrow(sql, capture_id)
        return None if rec is None else CaptureRow(**dict(rec))

    async def mark_deleted(self, capture_id: str) -> "CaptureRow | None":
        """Soft-delete: status='deleted' but the row remains for audit + GET 404."""
        sql = """
            UPDATE captures
            SET status = 'deleted', updated_at = NOW()
            WHERE id = $1
            RETURNING id, url, url_hash, source_app, shared_title, shared_text,
                      platform, status, doc_id, web_url, topic_path,
                      classifier_topic, classifier_conf, classifier_reasoning,
                      retry_count, created_at, extracted_snapshot
        """
        rec = await self._conn.fetchrow(sql, capture_id)
        return None if rec is None else CaptureRow(**dict(rec))


# ── Pool helpers ──────────────────────────────────────────────────────


def build_pool_kwargs(dsn: str, *, min_size: int = 1, max_size: int = 8) -> dict:
    """Return kwargs for `asyncpg.create_pool(...)`."""
    return {"dsn": dsn, "min_size": min_size, "max_size": max_size}


async def create_pool(dsn: str) -> asyncpg.Pool:
    """Create the asyncpg pool. Lifespan-managed by FastAPI in api.py."""
    return await asyncpg.create_pool(**build_pool_kwargs(dsn))


# ── Folder embeddings + topic aliases (Phase 5) ───────────────────────


@dataclass
class FolderEmbeddingRow:
    folder_id: str
    folder_name: str
    parent_path: str
    embedding: list[float]


_FOLDER_EMBEDDING_UPSERT_SQL = """
    INSERT INTO folder_embeddings (folder_id, folder_name, parent_path, embedding, updated_at)
    VALUES ($1, $2, $3, $4, NOW())
    ON CONFLICT (folder_id) DO UPDATE
        SET folder_name = EXCLUDED.folder_name,
            parent_path = EXCLUDED.parent_path,
            embedding   = EXCLUDED.embedding,
            updated_at  = NOW()
"""

_FOLDER_EMBEDDING_LIST_SQL = """
    SELECT folder_id, folder_name, parent_path, embedding, updated_at
    FROM folder_embeddings
    WHERE parent_path = $1
"""


def _format_pgvector(vec: list[float]) -> str:
    """pgvector's text representation: '[v1,v2,...]' with no spaces."""
    return "[" + ",".join(f"{v:.7g}" for v in vec) + "]"


def _parse_pgvector(s: "str | list[float]") -> list[float]:
    """Parse pgvector text or pass through if asyncpg returned a list."""
    if isinstance(s, list):
        return [float(v) for v in s]
    s = s.strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [float(x) for x in s.split(",") if x]


class FolderEmbeddingRepository:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def upsert(self, row: FolderEmbeddingRow) -> None:
        await self._conn.execute(
            _FOLDER_EMBEDDING_UPSERT_SQL,
            row.folder_id,
            row.folder_name,
            row.parent_path,
            _format_pgvector(row.embedding),
        )

    async def list_for_parent(self, parent_path: str) -> list[FolderEmbeddingRow]:
        records = await self._conn.fetch(_FOLDER_EMBEDDING_LIST_SQL, parent_path)
        return [
            FolderEmbeddingRow(
                folder_id=r["folder_id"],
                folder_name=r["folder_name"],
                parent_path=r["parent_path"],
                embedding=_parse_pgvector(r["embedding"]),
            )
            for r in records
        ]


_TOPIC_ALIAS_UPSERT_SQL = """
    INSERT INTO topic_aliases (parent_path, alias, canonical)
    VALUES ($1, $2, $3)
    ON CONFLICT (parent_path, alias) DO UPDATE
        SET canonical = EXCLUDED.canonical
"""

_TOPIC_ALIAS_LOOKUP_SQL = """
    SELECT canonical FROM topic_aliases WHERE parent_path = $1 AND alias = $2
"""


class TopicAliasRepository:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def record(self, *, parent_path: str, alias: str, canonical: str) -> None:
        await self._conn.execute(_TOPIC_ALIAS_UPSERT_SQL, parent_path, alias, canonical)

    async def lookup(self, *, parent_path: str, alias: str) -> str | None:
        return await self._conn.fetchval(_TOPIC_ALIAS_LOOKUP_SQL, parent_path, alias)
