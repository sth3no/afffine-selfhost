"""Content templates: model, repository, fallback resolver.

Templates are keyed by `(platform_id, topic)` where either field may be
`'*'` (wildcard). The fallback chain on `resolve()` is most-specific-first:

    (platform_id, topic)  →  (*, topic)  →  (platform_id, *)  →  (*, *)  →  None

The migration's seed `(*, *)` row guarantees the chain terminates without
synthesis for the default install. When the seed is deleted, resolve()
returns None and the caller is expected to fall through to the synthesizer.
"""

from __future__ import annotations

import json
from typing import Any

from ulid import ULID

from pydantic import AwareDatetime, BaseModel


class ContentTemplate(BaseModel):
    """In-memory model. Persisted as a row in `content_templates`."""

    id: str
    platform_id: str               # 'youtube', 'instagram', or '*'
    topic: str                     # 'Tutorials', 'Recipes', or '*'
    name: str
    system_prompt: str
    status: str                    # 'auto' | 'edited' | 'archived'
    generator_meta: dict[str, Any] | None = None
    created_by: str                # 'synth' | 'user'
    created_at: AwareDatetime
    updated_at: AwareDatetime


_RESOLVE_SQL = """
    SELECT id, platform_id, topic, name, system_prompt, status,
           generator_meta, created_by, created_at, updated_at
    FROM content_templates
    WHERE platform_id = $1 AND topic = $2 AND status <> 'archived'
    LIMIT 1
"""

_GET_SQL = """
    SELECT id, platform_id, topic, name, system_prompt, status,
           generator_meta, created_by, created_at, updated_at
    FROM content_templates
    WHERE id = $1
"""

_INSERT_SQL = """
    INSERT INTO content_templates
        (id, platform_id, topic, name, system_prompt, status, generator_meta, created_by)
    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
    RETURNING id, platform_id, topic, name, system_prompt, status,
              generator_meta, created_by, created_at, updated_at
"""

_INSERT_IF_ABSENT_SQL = """
    INSERT INTO content_templates
        (id, platform_id, topic, name, system_prompt, status, generator_meta, created_by)
    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
    ON CONFLICT (platform_id, topic) WHERE status <> 'archived' DO NOTHING
    RETURNING id, platform_id, topic, name, system_prompt, status,
              generator_meta, created_by, created_at, updated_at
"""

_COUNT_USAGE_SQL = """
    SELECT count(*) FROM captures WHERE template_id = $1
"""

_ARCHIVE_SQL = """
    UPDATE content_templates
    SET status = 'archived', updated_at = NOW()
    WHERE id = $1
    RETURNING id, platform_id, topic, name, system_prompt, status,
              generator_meta, created_by, created_at, updated_at
"""


class TemplatesRepository:
    """asyncpg-backed CRUD + fallback chain resolver.

    Constructed with a connection (or pool — duck-typed via .fetchrow/.fetch/.execute).
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def resolve(self, *, platform_id: str, topic: str) -> ContentTemplate | None:
        """Most-specific-first fallback chain."""
        for p, t in (
            (platform_id, topic),
            ("*", topic),
            (platform_id, "*"),
            ("*", "*"),
        ):
            record = await self._conn.fetchrow(_RESOLVE_SQL, p, t)
            if record is not None:
                return _row_to_model(record)
        return None

    async def get(self, *, template_id: str) -> ContentTemplate | None:
        record = await self._conn.fetchrow(_GET_SQL, template_id)
        return None if record is None else _row_to_model(record)

    async def list_all(
        self,
        *,
        platform_id: str | None = None,
        topic: str | None = None,
        status: str | None = None,
    ) -> list[ContentTemplate]:
        clauses: list[str] = []
        args: list[Any] = []
        if platform_id is not None:
            args.append(platform_id)
            clauses.append(f"platform_id = ${len(args)}")
        if topic is not None:
            args.append(topic)
            clauses.append(f"topic = ${len(args)}")
        if status is not None:
            args.append(status)
            clauses.append(f"status = ${len(args)}")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT id, platform_id, topic, name, system_prompt, status,
                   generator_meta, created_by, created_at, updated_at
            FROM content_templates
            {where}
            ORDER BY platform_id, topic
        """
        records = await self._conn.fetch(sql, *args)
        return [_row_to_model(r) for r in records]

    async def create(
        self,
        *,
        platform_id: str,
        topic: str,
        name: str,
        system_prompt: str,
        status: str = "edited",
        created_by: str = "user",
        generator_meta: dict[str, Any] | None = None,
    ) -> ContentTemplate:
        template_id = str(ULID())
        meta_json = json.dumps(generator_meta) if generator_meta is not None else None
        record = await self._conn.fetchrow(
            _INSERT_SQL,
            template_id, platform_id, topic, name, system_prompt,
            status, meta_json, created_by,
        )
        return _row_to_model(record)

    async def insert_if_absent(
        self,
        *,
        platform_id: str,
        topic: str,
        name: str,
        system_prompt: str,
        status: str = "auto",
        created_by: str = "synth",
        generator_meta: dict[str, Any] | None = None,
    ) -> ContentTemplate:
        """Concurrency-safe insert. If a row already exists at the active
        scope, returns it instead of failing."""
        template_id = str(ULID())
        meta_json = json.dumps(generator_meta) if generator_meta is not None else None
        record = await self._conn.fetchrow(
            _INSERT_IF_ABSENT_SQL,
            template_id, platform_id, topic, name, system_prompt,
            status, meta_json, created_by,
        )
        if record is not None:
            return _row_to_model(record)
        # Lost the race — read the winner's row.
        winner = await self._conn.fetchrow(_RESOLVE_SQL, platform_id, topic)
        if winner is None:
            raise RuntimeError(
                "insert_if_absent: ON CONFLICT returned no row AND no winner found"
            )
        return _row_to_model(winner)

    async def update(
        self,
        *,
        template_id: str,
        name: str | None = None,
        system_prompt: str | None = None,
        platform_id: str | None = None,
        topic: str | None = None,
    ) -> ContentTemplate | None:
        """Patch any of the fields. Setting system_prompt flips status to 'edited'
        if it was previously 'auto' (audit-trail signal for synth vs user edits)."""
        if name is None and system_prompt is None and platform_id is None and topic is None:
            raise ValueError("update() called with no fields to change")
        sets: list[str] = ["updated_at = NOW()"]
        args: list[Any] = []
        if name is not None:
            args.append(name)
            sets.append(f"name = ${len(args)}")
        if system_prompt is not None:
            args.append(system_prompt)
            sets.append(f"system_prompt = ${len(args)}")
            sets.append("status = CASE WHEN status = 'auto' THEN 'edited' ELSE status END")
        if platform_id is not None:
            args.append(platform_id)
            sets.append(f"platform_id = ${len(args)}")
        if topic is not None:
            args.append(topic)
            sets.append(f"topic = ${len(args)}")
        args.append(template_id)
        sql = f"""
            UPDATE content_templates
            SET {', '.join(sets)}
            WHERE id = ${len(args)}
            RETURNING id, platform_id, topic, name, system_prompt, status,
                      generator_meta, created_by, created_at, updated_at
        """
        record = await self._conn.fetchrow(sql, *args)
        return None if record is None else _row_to_model(record)

    async def archive(self, *, template_id: str) -> ContentTemplate | None:
        record = await self._conn.fetchrow(_ARCHIVE_SQL, template_id)
        return None if record is None else _row_to_model(record)

    async def count_usage(self, *, template_id: str) -> int:
        return int(await self._conn.fetchval(_COUNT_USAGE_SQL, template_id))


def _row_to_model(record: Any) -> ContentTemplate:
    """Map an asyncpg.Record (or dict from tests) to ContentTemplate."""
    d = dict(record)
    # asyncpg's default JSONB codec decodes to dict automatically, so the
    # isinstance check here is defensive — guards against custom codec
    # configurations and the test path which passes a JSON string for
    # fidelity with the on-disk JSONB shape.
    meta = d.get("generator_meta")
    if isinstance(meta, str):
        meta = json.loads(meta)
    return ContentTemplate(
        id=d["id"],
        platform_id=d["platform_id"],
        topic=d["topic"],
        name=d["name"],
        system_prompt=d["system_prompt"],
        status=d["status"],
        generator_meta=meta,
        created_by=d["created_by"],
        created_at=d["created_at"],
        updated_at=d["updated_at"],
    )
