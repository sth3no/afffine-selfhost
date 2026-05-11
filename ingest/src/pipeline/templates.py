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
