# Phase 14 — Content Templates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single fixed summarizer with per-`(platform_id, topic)` content templates. Templates are stored in Postgres, editable via new API endpoints, and synthesized by an LLM meta-prompt on first encounter. The renderer is extended to speak AFFiNE's full block vocabulary (code, mermaid, embed-html, callouts, keyframe refs, cross-doc refs).

**Spec:** [`docs/specs/2026-05-11-content-templates-design.md`](../specs/2026-05-11-content-templates-design.md)

**Architecture:**
1. `templates.py` — `ContentTemplate` model + `TemplatesRepository` with `(p,t) → (*,t) → (p,*) → (*,*)` fallback chain.
2. `template_synth.py` — Sonnet 4.6 meta-prompt that designs a system prompt for unknown (platform, topic) pairs; saves with `ON CONFLICT DO NOTHING`.
3. `templated_render.py` — Haiku call using the resolved template; returns `TemplatedOutput { title, lede, summary_md, body_md }`.
4. `markdown_render.py` — `markdown-it-py` parser → AFFiNE block specs; supports fenced code (any lang), mermaid, embed-html, callouts, `kf:<n>` keyframe refs, `[[Doc Title]]` cross-doc refs.
5. Orchestrator inserts one step between classify and file; saves `template_id` + prompt snapshot + output snapshot + extracted snapshot.
6. API adds 7 endpoints under `/templates/*` and `/captures/{id}/rerender`.

**Tech Stack:**
- `anthropic>=0.40` — Haiku 4.5 + Sonnet 4.6, both via `messages.parse(output_format=...)`
- `markdown-it-py>=3.0` — CommonMark parser with AST output (new dep)
- `asyncpg` — Postgres pool (existing)
- `pytest>=8.0` + `pytest-asyncio>=0.23` — test runner (existing)

**End-of-phase test count:** existing tests minus `test_summarizer.py` (~10 tests removed) plus ~65 new unit tests + 2 integration tests.

---

## Task 1: Migration 0002 — schema + seed

**Files:**
- Modify: `ingest/pyproject.toml` — add `markdown-it-py>=3.0`
- Create: `ingest/migrations/0002_content_templates.sql`
- Create: `ingest/tests/test_migration_0002.py`

- [ ] **Step 1.1: Add markdown-it-py to dependencies**

Edit [`ingest/pyproject.toml`](../../ingest/pyproject.toml). Insert into the `dependencies = [...]` list after `"Pillow>=10.0"`:

```toml
    "markdown-it-py>=3.0",
```

- [ ] **Step 1.2: Write the migration SQL**

Create `ingest/migrations/0002_content_templates.sql`:

```sql
-- Phase 14: content templates — per-(platform, topic) prompts.
-- Seed inserts the current summarizer prompt as the (*, *) default so
-- behavior on first deploy is identical to today.

CREATE TABLE IF NOT EXISTS content_templates (
    id              TEXT PRIMARY KEY,
    platform_id     TEXT NOT NULL,
    topic           TEXT NOT NULL,
    name            TEXT NOT NULL,
    system_prompt   TEXT NOT NULL,
    status          TEXT NOT NULL,
    generator_meta  JSONB,
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Active scope: only one non-archived template per (platform_id, topic).
CREATE UNIQUE INDEX IF NOT EXISTS content_templates_active_scope
    ON content_templates (platform_id, topic)
    WHERE status <> 'archived';

-- Lookup index for the fallback chain.
CREATE INDEX IF NOT EXISTS content_templates_scope_lookup
    ON content_templates (platform_id, topic)
    WHERE status <> 'archived';

-- Capture-level audit trail + replay inputs.
ALTER TABLE captures
    ADD COLUMN IF NOT EXISTS template_id          TEXT REFERENCES content_templates(id),
    ADD COLUMN IF NOT EXISTS template_prompt_used TEXT,
    ADD COLUMN IF NOT EXISTS template_output_raw  TEXT,
    ADD COLUMN IF NOT EXISTS extracted_snapshot   JSONB;

-- Seed the (*, *) default template — verbatim today's summarizer system prompt.
-- INSERT only when no (*, *) row exists, so re-running migrations is safe.
INSERT INTO content_templates (
    id, platform_id, topic, name, system_prompt, status, created_by
)
SELECT
    '01J5XYZ_SEED_DEFAULT'::text,  -- fixed seed ULID for idempotency
    '*',
    '*',
    'Default summarizer',
    $SEED$You are a content summarizer for a personal knowledge base.
For each captured social-media or web post, generate a concise descriptive
title and a punchy bulleted summary.

Title rules:
- 1-10 words, no URL, no enclosing brackets/quotes
- Capture the GIST of the source (e.g. "Travis Scott — Mavericks reel",
  "Italian carbonara recipe", "GPT-4 jailbreak demo")
- Title Case for English; sentence case for Czech/Slovak.

Summary rules:
- Markdown BULLETED LIST (3-6 items). Each bullet starts with "- " on its
  own line. One short punchy line per bullet.
- Highlight the most exciting, surprising, or actionable things in the
  content — what would catch someone's eye scanning their knowledge base?
- NO intro sentence, NO outro, NO sub-bullets, NO headings. Just the
  flat list.
- Don't restate metadata (duration, author, channel name) — that's
  rendered separately on the doc.
- If transcript is profane/explicit, summarize neutrally without
  reproducing slurs.

Lede rule:
- If the source title is a question, mystery, exaggeration, or clickbait
  teaser ("THEY DID IT", "This Changes Everything", "The Truth About X"),
  set `lede` to ONE direct sentence answering who/what/why. Otherwise
  leave `lede` null.

Description rule:
- If the source description is provided (publisher's video description,
  article byline), mine it for citations, source links, chapter markers,
  related content. Surface valuable references inside `body_md` (typically
  as a `## Sources` section). Strip sponsor/social noise.

Language rules:
- Default output language is ENGLISH for both title and summary —
  translate from any source language.
- EXCEPTION: if the source content is Czech or Slovak, keep BOTH the
  title and summary in the original Czech/Slovak. Don't translate.

Body rule (default template):
- `body_md` is a freeform markdown body. For the default template, just
  echo the most informative section of the source (transcript, article
  body) under a `## Content` heading. Specialized templates override this.

Return STRICT JSON matching the TemplatedOutput schema only — no prose,
no markdown code fences.
$SEED$,
    'auto',
    'synth'
WHERE NOT EXISTS (
    SELECT 1 FROM content_templates
    WHERE platform_id = '*' AND topic = '*' AND status <> 'archived'
);
```

- [ ] **Step 1.3: Write the failing test**

Create `ingest/tests/test_migration_0002.py`:

```python
"""Integration test for migration 0002 (content_templates + seed).

Skipped unless DB_ADMIN_URL is set (CI provides it; local dev opts in
via .env). Runs both 0001 and 0002 on a throwaway database, then asserts
the schema and seed row.
"""

import os
import uuid

import asyncpg
import pytest

from src.migrate import apply_migrations, ensure_database

pytestmark = pytest.mark.skipif(
    not os.environ.get("DB_ADMIN_URL"),
    reason="DB_ADMIN_URL not set",
)


@pytest.fixture
async def throwaway_db():
    """Create a fresh database, apply migrations, drop after."""
    admin = os.environ["DB_ADMIN_URL"]
    name = f"ingest_test_{uuid.uuid4().hex[:8]}"

    conn = await asyncpg.connect(admin)
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()

    target = admin.rsplit("/", 1)[0] + f"/{name}"
    await apply_migrations(target)
    yield target

    conn = await asyncpg.connect(admin)
    try:
        await conn.execute(f'DROP DATABASE "{name}"')
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_seed_default_template_present(throwaway_db):
    conn = await asyncpg.connect(throwaway_db)
    try:
        row = await conn.fetchrow(
            "SELECT id, platform_id, topic, status, created_by "
            "FROM content_templates WHERE platform_id='*' AND topic='*'"
        )
    finally:
        await conn.close()
    assert row is not None
    assert row["status"] == "auto"
    assert row["created_by"] == "synth"


@pytest.mark.asyncio
async def test_captures_has_new_columns(throwaway_db):
    conn = await asyncpg.connect(throwaway_db)
    try:
        cols = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='captures'"
        )
    finally:
        await conn.close()
    names = {r["column_name"] for r in cols}
    assert "template_id" in names
    assert "template_prompt_used" in names
    assert "template_output_raw" in names
    assert "extracted_snapshot" in names


@pytest.mark.asyncio
async def test_migration_is_idempotent(throwaway_db):
    """Re-running migrations doesn't duplicate the seed or fail."""
    await apply_migrations(throwaway_db)  # second run
    conn = await asyncpg.connect(throwaway_db)
    try:
        count = await conn.fetchval(
            "SELECT count(*) FROM content_templates "
            "WHERE platform_id='*' AND topic='*'"
        )
    finally:
        await conn.close()
    assert count == 1


@pytest.mark.asyncio
async def test_unique_active_scope_enforced(throwaway_db):
    """Two non-archived rows with same (platform_id, topic) are rejected."""
    conn = await asyncpg.connect(throwaway_db)
    try:
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO content_templates "
                "(id, platform_id, topic, name, system_prompt, status, created_by) "
                "VALUES ('dup', '*', '*', 'dup', 'x', 'edited', 'user')"
            )
        # Archived dup is allowed (excluded from the partial unique index).
        await conn.execute(
            "INSERT INTO content_templates "
            "(id, platform_id, topic, name, system_prompt, status, created_by) "
            "VALUES ('arch', '*', '*', 'arch', 'x', 'archived', 'user')"
        )
    finally:
        await conn.close()
```

- [ ] **Step 1.4: Run test (expected: fail — migration file missing on disk → migration runner errors)**

```
cd ingest && python -m pytest tests/test_migration_0002.py -v
```

Expected: FAIL (migration 0002 not yet applied because the file isn't picked up; or all four tests fail because table/columns don't exist).

- [ ] **Step 1.5: Run test (expected: pass)**

After steps 1.1–1.2 the migration file exists and is picked up by the lexical-order runner in [`src/migrate.py`](../../ingest/src/migrate.py).

```
cd ingest && python -m pytest tests/test_migration_0002.py -v
```

Expected: all 4 tests PASS (when `DB_ADMIN_URL` set; otherwise skipped).

- [ ] **Step 1.6: Commit**

```bash
git add ingest/pyproject.toml ingest/migrations/0002_content_templates.sql ingest/tests/test_migration_0002.py
git commit -m "feat(ingest): migration 0002 — content_templates table + seed (*, *) default"
```

---

## Task 2: ContentTemplate model + TemplatesRepository.resolve()

**Files:**
- Create: `ingest/src/pipeline/templates.py`
- Create: `ingest/tests/test_templates_repo.py`

- [ ] **Step 2.1: Write the failing test**

Create `ingest/tests/test_templates_repo.py`:

```python
"""Tests for ContentTemplate model + TemplatesRepository.resolve().

Uses an in-memory dict-backed fake connection (not pytest-asyncpg) so the
unit test doesn't require a running Postgres. Real DB integration is
covered by tests under conftest.py with a live pool.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.pipeline.templates import ContentTemplate, TemplatesRepository


def _row(platform_id: str, topic: str, status: str = "edited", template_id: str | None = None):
    return {
        "id": template_id or f"t_{platform_id}_{topic}",
        "platform_id": platform_id,
        "topic": topic,
        "name": f"{platform_id}/{topic}",
        "system_prompt": "test prompt",
        "status": status,
        "generator_meta": None,
        "created_by": "user",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }


@pytest.mark.asyncio
async def test_resolve_prefers_exact_match():
    conn = AsyncMock()
    conn.fetchrow.side_effect = [_row("youtube", "Tutorials")]
    repo = TemplatesRepository(conn)

    tmpl = await repo.resolve(platform_id="youtube", topic="Tutorials")

    assert tmpl is not None
    assert tmpl.platform_id == "youtube"
    assert tmpl.topic == "Tutorials"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_topic_wildcard():
    """No (youtube, Recipes) → use (*, Recipes)."""
    conn = AsyncMock()
    conn.fetchrow.side_effect = [None, _row("*", "Recipes")]
    repo = TemplatesRepository(conn)

    tmpl = await repo.resolve(platform_id="youtube", topic="Recipes")

    assert tmpl is not None
    assert tmpl.platform_id == "*"
    assert tmpl.topic == "Recipes"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_platform_wildcard():
    """No (instagram, AI) and no (*, AI) → use (instagram, *)."""
    conn = AsyncMock()
    conn.fetchrow.side_effect = [None, None, _row("instagram", "*")]
    repo = TemplatesRepository(conn)

    tmpl = await repo.resolve(platform_id="instagram", topic="AI")

    assert tmpl is not None
    assert tmpl.platform_id == "instagram"
    assert tmpl.topic == "*"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_global_default():
    """All specific lookups miss → use (*, *)."""
    conn = AsyncMock()
    conn.fetchrow.side_effect = [None, None, None, _row("*", "*")]
    repo = TemplatesRepository(conn)

    tmpl = await repo.resolve(platform_id="reddit", topic="Politics")

    assert tmpl is not None
    assert tmpl.platform_id == "*"
    assert tmpl.topic == "*"


@pytest.mark.asyncio
async def test_resolve_returns_none_when_nothing_matches():
    conn = AsyncMock()
    conn.fetchrow.side_effect = [None, None, None, None]
    repo = TemplatesRepository(conn)

    tmpl = await repo.resolve(platform_id="x", topic="Memes")

    assert tmpl is None


@pytest.mark.asyncio
async def test_resolve_skips_archived_rows():
    """Archived rows aren't picked up by the SQL filter (WHERE status <> 'archived').
    The repo just trusts the SQL; this test asserts the SQL contains the filter."""
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    repo = TemplatesRepository(conn)

    await repo.resolve(platform_id="youtube", topic="Tutorials")

    # Inspect the first SQL call: must filter on status.
    first_call_sql = conn.fetchrow.await_args_list[0].args[0]
    assert "status <> 'archived'" in first_call_sql or "status != 'archived'" in first_call_sql
```

- [ ] **Step 2.2: Run test (expected: fail — `templates` module doesn't exist)**

```
cd ingest && python -m pytest tests/test_templates_repo.py -v
```

Expected: FAIL with `ModuleNotFoundError: src.pipeline.templates`.

- [ ] **Step 2.3: Implement `templates.py`**

Create `ingest/src/pipeline/templates.py`:

```python
"""Content templates: model, repository, fallback resolver.

Templates are keyed by `(platform_id, topic)` where either field may be
`'*'` (wildcard). The fallback chain on `resolve()` is most-specific-first:

    (platform_id, topic)  →  (*, topic)  →  (platform_id, *)  →  (*, *)  →  None

The migration's seed `(*, *)` row guarantees the chain terminates without
synthesis for the default install. When the seed is deleted, resolve()
returns None and the caller is expected to fall through to the synthesizer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


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
    created_at: datetime
    updated_at: datetime


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
    # generator_meta arrives from asyncpg as a JSON string when the column is
    # JSONB; coerce to dict for the model. None passes through.
    import json
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
```

- [ ] **Step 2.4: Run test (expected: pass)**

```
cd ingest && python -m pytest tests/test_templates_repo.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 2.5: Commit**

```bash
git add ingest/src/pipeline/templates.py ingest/tests/test_templates_repo.py
git commit -m "feat(ingest): templates.py — ContentTemplate model + fallback resolver"
```

---

## Task 3: Rest of TemplatesRepository CRUD

**Files:**
- Modify: `ingest/src/pipeline/templates.py`
- Modify: `ingest/tests/test_templates_repo.py`

- [ ] **Step 3.1: Write failing tests for CRUD methods**

Append to `ingest/tests/test_templates_repo.py`:

```python
from src.pipeline.templates import TemplatesRepository


@pytest.mark.asyncio
async def test_get_returns_template_by_id():
    conn = AsyncMock()
    conn.fetchrow.return_value = _row("youtube", "Tutorials", template_id="t_yt_tut")
    repo = TemplatesRepository(conn)

    tmpl = await repo.get(template_id="t_yt_tut")

    assert tmpl is not None
    assert tmpl.id == "t_yt_tut"


@pytest.mark.asyncio
async def test_get_returns_none_when_missing():
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    repo = TemplatesRepository(conn)
    assert await repo.get(template_id="nope") is None


@pytest.mark.asyncio
async def test_list_with_no_filters():
    conn = AsyncMock()
    conn.fetch.return_value = [_row("youtube", "Tutorials"), _row("*", "*")]
    repo = TemplatesRepository(conn)

    rows = await repo.list_all()

    assert len(rows) == 2


@pytest.mark.asyncio
async def test_list_filters_by_platform_and_status():
    conn = AsyncMock()
    conn.fetch.return_value = [_row("youtube", "Tutorials", status="edited")]
    repo = TemplatesRepository(conn)

    rows = await repo.list_all(platform_id="youtube", status="edited")

    assert len(rows) == 1
    # Inspect SQL to ensure WHERE clauses applied:
    sql = conn.fetch.await_args.args[0]
    assert "platform_id" in sql
    assert "status" in sql


@pytest.mark.asyncio
async def test_create_inserts_and_returns():
    conn = AsyncMock()
    inserted = _row("youtube", "Tutorials", status="edited")
    conn.fetchrow.return_value = inserted
    repo = TemplatesRepository(conn)

    tmpl = await repo.create(
        platform_id="youtube",
        topic="Tutorials",
        name="YouTube Tutorial v1",
        system_prompt="prompt",
        status="edited",
        created_by="user",
        generator_meta=None,
    )

    assert tmpl.id == inserted["id"]
    # fetchrow used because we RETURN the inserted row.
    assert conn.fetchrow.await_count == 1


@pytest.mark.asyncio
async def test_update_changes_status_to_edited_when_prompt_changes():
    """PUT /templates/{id} with new system_prompt flips status auto → edited."""
    conn = AsyncMock()
    updated = _row("youtube", "Tutorials", status="edited")
    conn.fetchrow.return_value = updated
    repo = TemplatesRepository(conn)

    tmpl = await repo.update(template_id="t_yt_tut", system_prompt="new")

    assert tmpl is not None
    sql = conn.fetchrow.await_args.args[0]
    assert "status" in sql


@pytest.mark.asyncio
async def test_archive_soft_deletes():
    conn = AsyncMock()
    archived = _row("youtube", "Tutorials", status="archived")
    conn.fetchrow.return_value = archived
    repo = TemplatesRepository(conn)

    tmpl = await repo.archive(template_id="t_yt_tut")

    assert tmpl is not None
    assert tmpl.status == "archived"


@pytest.mark.asyncio
async def test_count_usage_returns_int():
    conn = AsyncMock()
    conn.fetchval.return_value = 42
    repo = TemplatesRepository(conn)

    count = await repo.count_usage(template_id="t_yt_tut")

    assert count == 42


@pytest.mark.asyncio
async def test_insert_if_absent_on_conflict_does_nothing():
    """Synthesis race: two concurrent synth calls. The second returns the
    existing row rather than failing."""
    conn = AsyncMock()
    # ON CONFLICT DO NOTHING returns no row → fall back to a SELECT
    conn.fetchrow.side_effect = [None, _row("youtube", "AI")]
    repo = TemplatesRepository(conn)

    tmpl = await repo.insert_if_absent(
        platform_id="youtube",
        topic="AI",
        name="x",
        system_prompt="x",
        status="auto",
        created_by="synth",
        generator_meta={"biggest_value": "..."},
    )

    assert tmpl is not None
    assert tmpl.platform_id == "youtube"
```

- [ ] **Step 3.2: Run test (expected: fail — methods missing)**

```
cd ingest && python -m pytest tests/test_templates_repo.py -v
```

Expected: ~9 FAILS with `AttributeError: 'TemplatesRepository' object has no attribute 'get'` etc.

- [ ] **Step 3.3: Implement CRUD methods**

Append to `ingest/src/pipeline/templates.py`:

```python
import json

from ulid import ULID


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


class TemplatesRepository(TemplatesRepository):  # noqa: F811 — re-open
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
        sets: list[str] = ["updated_at = NOW()"]
        args: list[Any] = []
        if name is not None:
            args.append(name)
            sets.append(f"name = ${len(args)}")
        if system_prompt is not None:
            args.append(system_prompt)
            sets.append(f"system_prompt = ${len(args)}")
            # Promote auto → edited so the user can see at a glance which
            # templates have been tuned vs left at synth defaults.
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
        record = await self._conn.fetchrow(
            """
            UPDATE content_templates
            SET status = 'archived', updated_at = NOW()
            WHERE id = $1
            RETURNING id, platform_id, topic, name, system_prompt, status,
                      generator_meta, created_by, created_at, updated_at
            """,
            template_id,
        )
        return None if record is None else _row_to_model(record)

    async def count_usage(self, *, template_id: str) -> int:
        return int(await self._conn.fetchval(_COUNT_USAGE_SQL, template_id))
```

Then merge into the original class instead of subclassing. Move all method bodies into the original `class TemplatesRepository:` block defined in Step 2.3 and delete the `class TemplatesRepository(TemplatesRepository):` shim. The final file has one class with all methods.

- [ ] **Step 3.4: Run test (expected: pass)**

```
cd ingest && python -m pytest tests/test_templates_repo.py -v
```

Expected: all ~15 tests PASS.

- [ ] **Step 3.5: Commit**

```bash
git add ingest/src/pipeline/templates.py ingest/tests/test_templates_repo.py
git commit -m "feat(ingest): templates.py — full CRUD (get/list/create/update/archive/usage)"
```

---

## Task 4: TemplatedOutput model + templated_render.render()

**Files:**
- Create: `ingest/src/pipeline/templated_render.py`
- Create: `ingest/tests/test_templated_render.py`

- [ ] **Step 4.1: Write the failing test**

Create `ingest/tests/test_templated_render.py`:

```python
"""Tests for the templated Haiku render call."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.pipeline.extracted import Extracted, MediaKind
from src.pipeline.templates import ContentTemplate
from src.pipeline.templated_render import TemplatedOutput, fallback_title, render


def _extracted(**overrides) -> Extracted:
    base = dict(
        title="Some Title",
        body_md="Body content.",
        author="@channel",
        published_at=None,
        media_kind=MediaKind.VIDEO,
        extra={},
    )
    base.update(overrides)
    return Extracted(**base)


def _template(**overrides) -> ContentTemplate:
    from datetime import datetime, timezone
    base = dict(
        id="t_test",
        platform_id="youtube",
        topic="Tutorials",
        name="YouTube Tutorial v1",
        system_prompt="You are a tutorial summarizer. Produce numbered steps.",
        status="edited",
        generator_meta=None,
        created_by="user",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return ContentTemplate(**base)


# ── Pure helpers ────────────────────────────────────────────────────


def test_fallback_title_prefers_extracted_title():
    e = _extracted(title="My Title", author=None)
    assert fallback_title(e, url=None) == "My Title"


def test_fallback_title_uses_url_host():
    e = _extracted(title=None, author=None)
    assert fallback_title(e, url="https://www.instagram.com/reel/abc/?x=1") == "Capture from www.instagram.com"


# ── render() shape ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_render_returns_templated_output():
    fake = MagicMock()
    fake.parsed_output = TemplatedOutput(
        title="React Hooks Tutorial",
        lede="useEffect runs after every render unless deps are passed.",
        summary_md="- Hooks replace class lifecycle\n- useState manages local state",
        body_md="## Steps\n1. Import useState\n2. Call inside the component",
    )

    with patch("src.pipeline.templated_render.AsyncAnthropic") as Client, \
         patch("src.pipeline.templated_render.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-haiku-4-5"
        settings_mock.summarizer_max_body_chars = 4000
        Client.return_value.messages.parse = AsyncMock(return_value=fake)

        result = await render(template=_template(), extracted=_extracted(), keyframes=[])

    assert isinstance(result, TemplatedOutput)
    assert result.title == "React Hooks Tutorial"
    assert result.lede is not None
    assert "useState" in result.body_md


@pytest.mark.asyncio
async def test_render_uses_template_system_prompt():
    """The template's system_prompt is what gets sent — not a hardcoded one."""
    fake = MagicMock()
    fake.parsed_output = TemplatedOutput(title="X", lede=None, summary_md="- a", body_md="b")

    with patch("src.pipeline.templated_render.AsyncAnthropic") as Client, \
         patch("src.pipeline.templated_render.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-haiku-4-5"
        settings_mock.summarizer_max_body_chars = 4000
        instance = Client.return_value
        instance.messages.parse = AsyncMock(return_value=fake)

        await render(
            template=_template(system_prompt="UNIQUE_MARKER_PROMPT_TEXT"),
            extracted=_extracted(),
            keyframes=[],
        )

    call = instance.messages.parse.await_args
    system = call.kwargs["system"]
    assert system[0]["text"] == "UNIQUE_MARKER_PROMPT_TEXT"
    assert system[0].get("cache_control") == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_render_includes_description_in_user_message():
    """Description from extractor.extra is surfaced — sources/citations signal."""
    fake = MagicMock()
    fake.parsed_output = TemplatedOutput(title="X", lede=None, summary_md="- a", body_md="b")

    with patch("src.pipeline.templated_render.AsyncAnthropic") as Client, \
         patch("src.pipeline.templated_render.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-haiku-4-5"
        settings_mock.summarizer_max_body_chars = 4000
        instance = Client.return_value
        instance.messages.parse = AsyncMock(return_value=fake)

        await render(
            template=_template(),
            extracted=_extracted(extra={
                "description": "Sources: https://example.com/paper.pdf. Chapter 1: 0:00.",
            }),
            keyframes=[],
        )

    user_msg = instance.messages.parse.await_args.kwargs["messages"][0]["content"]
    assert "Source description" in user_msg
    assert "example.com/paper.pdf" in user_msg


@pytest.mark.asyncio
async def test_render_includes_keyframes_in_user_message():
    fake = MagicMock()
    fake.parsed_output = TemplatedOutput(title="X", lede=None, summary_md="- a", body_md="b")
    keyframes = [
        {"timestamp_seconds": 42.3, "caption": "IDE with React code", "blob_source_id": "blob1"},
        {"timestamp_seconds": 154.0, "caption": "Network tab 200 OK", "blob_source_id": "blob2"},
    ]

    with patch("src.pipeline.templated_render.AsyncAnthropic") as Client, \
         patch("src.pipeline.templated_render.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-haiku-4-5"
        settings_mock.summarizer_max_body_chars = 4000
        instance = Client.return_value
        instance.messages.parse = AsyncMock(return_value=fake)

        await render(template=_template(), extracted=_extracted(), keyframes=keyframes)

    user_msg = instance.messages.parse.await_args.kwargs["messages"][0]["content"]
    assert "Available keyframes" in user_msg
    assert "[0]" in user_msg
    assert "IDE with React code" in user_msg
    assert "[1]" in user_msg
    assert "kf:" in user_msg  # syntax hint to template


@pytest.mark.asyncio
async def test_render_includes_video_summary_when_present():
    fake = MagicMock()
    fake.parsed_output = TemplatedOutput(title="X", lede=None, summary_md="- a", body_md="b")

    with patch("src.pipeline.templated_render.AsyncAnthropic") as Client, \
         patch("src.pipeline.templated_render.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-haiku-4-5"
        settings_mock.summarizer_max_body_chars = 4000
        instance = Client.return_value
        instance.messages.parse = AsyncMock(return_value=fake)

        await render(
            template=_template(),
            extracted=_extracted(extra={"video_summary": "Streaming demo content."}),
            keyframes=[],
        )

    user_msg = instance.messages.parse.await_args.kwargs["messages"][0]["content"]
    assert "Vision-grounded summary" in user_msg
    assert "Streaming demo" in user_msg


@pytest.mark.asyncio
async def test_render_truncates_long_body():
    fake = MagicMock()
    fake.parsed_output = TemplatedOutput(title="X", lede=None, summary_md="- a", body_md="b")

    with patch("src.pipeline.templated_render.AsyncAnthropic") as Client, \
         patch("src.pipeline.templated_render.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-haiku-4-5"
        settings_mock.summarizer_max_body_chars = 4000
        instance = Client.return_value
        instance.messages.parse = AsyncMock(return_value=fake)

        await render(
            template=_template(),
            extracted=_extracted(body_md="X" * 100_000),
            keyframes=[],
        )

    user_msg = instance.messages.parse.await_args.kwargs["messages"][0]["content"]
    assert "first 4000 chars" in user_msg
    assert len(user_msg) < 8000


@pytest.mark.asyncio
async def test_render_raises_when_parsed_output_is_none():
    fake = MagicMock()
    fake.parsed_output = None

    with patch("src.pipeline.templated_render.AsyncAnthropic") as Client, \
         patch("src.pipeline.templated_render.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-haiku-4-5"
        settings_mock.summarizer_max_body_chars = 4000
        Client.return_value.messages.parse = AsyncMock(return_value=fake)

        with pytest.raises(RuntimeError, match="parsed_output is None"):
            await render(template=_template(), extracted=_extracted(), keyframes=[])
```

- [ ] **Step 4.2: Run test (expected: fail — module missing)**

```
cd ingest && python -m pytest tests/test_templated_render.py -v
```

Expected: FAIL with `ModuleNotFoundError: src.pipeline.templated_render`.

- [ ] **Step 4.3: Implement `templated_render.py`**

Create `ingest/src/pipeline/templated_render.py`:

```python
"""Templated render call — Haiku 4.5 with a per-template system prompt.

Replaces the fixed `summarizer.py`. The template comes from the
`content_templates` table (or LLM synthesis); its `system_prompt` is
sent verbatim with `cache_control: ephemeral` so the prefix cache hits
across consecutive captures of the same kind.

Returns a strict `TemplatedOutput { title, lede, summary_md, body_md }`
via `messages.parse(output_format=TemplatedOutput)`.
"""

from __future__ import annotations

import logging
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field

from src.config import settings
from src.pipeline.extracted import Extracted
from src.pipeline.templates import ContentTemplate

log = logging.getLogger(__name__)


class TemplatedOutput(BaseModel):
    """Strict shape Claude must return — enforced by structured-outputs."""

    title: str = Field(
        description=(
            "Short descriptive title (1-10 words). No URL, no brackets. "
            "Default language ENGLISH; if the source content is Czech or "
            "Slovak, keep the title in that language."
        ),
    )
    lede: str | None = Field(
        default=None,
        description=(
            "ONE sentence that directly answers a clickbait/teaser title "
            "(who/what/why). Populate when the source title is a question, "
            "mystery, exaggeration, or clickbait. Otherwise leave null."
        ),
    )
    summary_md: str = Field(
        description=(
            "Markdown bulleted list (3-6 items) of the most exciting, "
            "surprising, or actionable things. Each bullet on its own line, "
            "starts with '- '. NO intro/outro prose."
        ),
    )
    body_md: str = Field(
        description=(
            "Template-specific structured markdown body. Headings, lists, "
            "code blocks, mermaid, embed-html, kf:<n> image refs, "
            "[[Doc Title]] cross-refs all allowed. Rendered downstream."
        ),
    )


def _build_user_message(extracted: Extracted, keyframes: list[dict[str, Any]]) -> str:
    body_excerpt = (extracted.body_md or "")[: settings.summarizer_max_body_chars]
    description = (extracted.extra or {}).get("description")
    video_summary = (extracted.extra or {}).get("video_summary")
    published = getattr(extracted, "published_at", None)

    parts: list[str] = [
        "Captured content:",
        f"- Original title: {extracted.title or '(none)'}",
        f"- Author/channel: {extracted.author or '(unknown)'}",
        f"- Media kind: {extracted.media_kind.value}",
        f"- Published: {published or '(unknown)'}",
        "",
    ]

    if description:
        parts.append(
            "Source description (from publisher — may contain sources, "
            "chapter markers, sponsor links, related content; extract valuable "
            "references, strip noise):"
        )
        parts.append(str(description))
        parts.append("")

    if video_summary:
        parts.append("Vision-grounded summary (transcript + keyframes):")
        parts.append(str(video_summary))
        parts.append("")

    if keyframes:
        parts.append(
            "Available keyframes (reference by index, e.g. ![caption](kf:2)):"
        )
        for i, kf in enumerate(keyframes):
            ts = kf.get("timestamp_seconds", 0.0)
            caption = (kf.get("caption") or "").strip()
            parts.append(f"  [{i}] t={ts:.1f}s — {caption}")
        parts.append("")

    parts.append(
        f"Body excerpt (truncated to first {settings.summarizer_max_body_chars} chars):"
    )
    parts.append("")
    parts.append(body_excerpt)
    return "\n".join(parts)


async def render(
    *,
    template: ContentTemplate,
    extracted: Extracted,
    keyframes: list[dict[str, Any]],
) -> TemplatedOutput:
    """Single Haiku call → TemplatedOutput."""
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    user_msg = _build_user_message(extracted, keyframes)

    response = await client.messages.parse(
        model=settings.summarizer_model,
        max_tokens=2048,
        system=[
            {
                "type": "text",
                "text": template.system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_msg}],
        output_format=TemplatedOutput,
    )

    if response.parsed_output is None:
        raise RuntimeError(
            "templated_render: parsed_output is None — schema-enforced parse "
            "failed; check summarizer_model supports structured outputs"
        )
    return response.parsed_output


def fallback_title(extracted: Extracted, *, url: str | None) -> str:
    """Deterministic title when no API key / parse fails. Moved from summarizer.py."""
    if extracted.title:
        return extracted.title.strip()
    if extracted.author:
        return f"{extracted.author} — {extracted.media_kind.value}"
    if url:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or url
        return f"Capture from {host}"
    return "Untitled capture"
```

- [ ] **Step 4.4: Run test (expected: pass)**

```
cd ingest && python -m pytest tests/test_templated_render.py -v
```

Expected: all ~9 tests PASS.

- [ ] **Step 4.5: Commit**

```bash
git add ingest/src/pipeline/templated_render.py ingest/tests/test_templated_render.py
git commit -m "feat(ingest): templated_render.py — Haiku call with per-template prompt"
```

---

## Task 5: template_synth.synthesize_template()

**Files:**
- Create: `ingest/src/pipeline/template_synth.py`
- Create: `ingest/tests/test_template_synth.py`

- [ ] **Step 5.1: Write the failing test**

Create `ingest/tests/test_template_synth.py`:

```python
"""Tests for the LLM template synthesizer (Sonnet meta-prompt)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.pipeline.extracted import Extracted, MediaKind
from src.pipeline.templates import ContentTemplate
from src.pipeline.template_synth import (
    SynthesizedTemplate,
    META_SYSTEM_PROMPT,
    synthesize_template,
)


def _extracted(**overrides) -> Extracted:
    from datetime import datetime, timezone
    base = dict(
        title="How to bake sourdough",
        body_md="Mix flour, water, starter. Bulk ferment 4-6 hours...",
        author="Bakery",
        published_at=None,
        media_kind=MediaKind.VIDEO,
        extra={"description": "Full recipe and ratios in the description."},
    )
    base.update(overrides)
    return Extracted(**base)


def _template(**overrides) -> ContentTemplate:
    from datetime import datetime, timezone
    base = dict(
        id="t_new",
        platform_id="youtube",
        topic="Recipes",
        name="YouTube Recipe v1",
        system_prompt="You are a recipe summarizer.",
        status="auto",
        generator_meta={"biggest_value": "ingredients + steps"},
        created_by="synth",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return ContentTemplate(**base)


def test_meta_prompt_mentions_required_blocks():
    """The meta-prompt teaches the synthesizer what AFFiNE blocks are
    available. Sanity-check it lists the key flavours."""
    assert "mermaid" in META_SYSTEM_PROMPT.lower()
    assert "embed-html" in META_SYSTEM_PROMPT.lower()
    assert "callout" in META_SYSTEM_PROMPT.lower()
    assert "kf:" in META_SYSTEM_PROMPT
    assert "[[Doc Title]]" in META_SYSTEM_PROMPT or "[[" in META_SYSTEM_PROMPT
    assert "lede" in META_SYSTEM_PROMPT.lower()


@pytest.mark.asyncio
async def test_synthesize_template_calls_sonnet_and_inserts_row():
    fake = MagicMock()
    fake.parsed_output = SynthesizedTemplate(
        name="YouTube Recipe v1",
        system_prompt="You are a recipe summarizer. ...",
        biggest_value="Ingredients + numbered steps.",
        user_intent="Cook it later.",
        best_roi_format="Ingredients list + numbered steps + time estimate.",
        available_blocks_used=["paragraph", "list", "callout"],
    )
    repo = AsyncMock()
    repo.insert_if_absent = AsyncMock(return_value=_template(platform_id="youtube", topic="Recipes"))

    with patch("src.pipeline.template_synth.AsyncAnthropic") as Client, \
         patch("src.pipeline.template_synth.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.vision_model = "claude-sonnet-4-6"
        Client.return_value.messages.parse = AsyncMock(return_value=fake)

        tmpl = await synthesize_template(
            platform_id="youtube",
            topic="Recipes",
            sample_extracted=_extracted(),
            templates_repo=repo,
        )

    assert tmpl is not None
    assert tmpl.platform_id == "youtube"
    assert tmpl.topic == "Recipes"
    # Sonnet model used for synthesis (not Haiku).
    call = Client.return_value.messages.parse.await_args
    assert call.kwargs["model"] == "claude-sonnet-4-6"
    # Repo received the synthesized prompt + generator_meta.
    repo.insert_if_absent.assert_awaited_once()
    kwargs = repo.insert_if_absent.await_args.kwargs
    assert kwargs["platform_id"] == "youtube"
    assert kwargs["topic"] == "Recipes"
    assert kwargs["system_prompt"] == "You are a recipe summarizer. ..."
    assert kwargs["created_by"] == "synth"
    assert kwargs["status"] == "auto"
    assert "biggest_value" in kwargs["generator_meta"]


@pytest.mark.asyncio
async def test_synthesize_template_passes_sample_in_user_message():
    fake = MagicMock()
    fake.parsed_output = SynthesizedTemplate(
        name="X", system_prompt="x",
        biggest_value="x", user_intent="x", best_roi_format="x",
        available_blocks_used=["paragraph"],
    )
    repo = AsyncMock()
    repo.insert_if_absent = AsyncMock(return_value=_template())

    with patch("src.pipeline.template_synth.AsyncAnthropic") as Client, \
         patch("src.pipeline.template_synth.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.vision_model = "claude-sonnet-4-6"
        instance = Client.return_value
        instance.messages.parse = AsyncMock(return_value=fake)

        await synthesize_template(
            platform_id="youtube",
            topic="Recipes",
            sample_extracted=_extracted(title="UNIQUE_SAMPLE_TITLE_TOKEN"),
            templates_repo=repo,
        )

    user_msg = instance.messages.parse.await_args.kwargs["messages"][0]["content"]
    assert "youtube" in user_msg
    assert "Recipes" in user_msg
    assert "UNIQUE_SAMPLE_TITLE_TOKEN" in user_msg
```

- [ ] **Step 5.2: Run test (expected: fail — module missing)**

```
cd ingest && python -m pytest tests/test_template_synth.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 5.3: Implement `template_synth.py`**

Create `ingest/src/pipeline/template_synth.py`:

```python
"""Template synthesizer — Sonnet 4.6 meta-prompt designs a template
for a (platform, topic) pair we haven't seen before.

Called from the orchestrator when `TemplatesRepository.resolve()` returns
None at every fallback level. Saves with `insert_if_absent` so concurrent
synthesis races resolve to a single winner.
"""

from __future__ import annotations

import logging

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field

from src.config import settings
from src.pipeline.extracted import Extracted
from src.pipeline.templates import ContentTemplate, TemplatesRepository

log = logging.getLogger(__name__)


class SynthesizedTemplate(BaseModel):
    """Sonnet's output: a name + the system prompt to use for this scope,
    plus reflective fields stored on `generator_meta` for audit."""

    name: str = Field(description="Human-readable label, 2-6 words.")
    system_prompt: str = Field(
        description="The complete system prompt that Haiku will receive for every "
                    "future capture matching this (platform, topic) scope. Self-contained."
    )
    biggest_value: str = Field(description="What's the biggest value in this kind of content?")
    user_intent: str = Field(description="What does the user actually want when they save one?")
    best_roi_format: str = Field(description="Best ROI format — what should body_md look like?")
    available_blocks_used: list[str] = Field(
        description="Which AFFiNE block flavours the generated prompt actively instructs."
    )


META_SYSTEM_PROMPT = """You are designing a content template for a personal
knowledge-base ingestion pipeline. Each captured URL of a given
(platform, topic) kind will be summarized into an AFFiNE document. Your
job: design the system prompt that will run for every future capture
matching this scope.

You will be given:
- The platform (e.g., youtube, instagram, arxiv)
- The topic (e.g., Tutorials, Recipes, Documentary)
- One sample capture's extracted content (title, author, description,
  transcript/body, vision summary if present, keyframes available)

Ask yourself, in this order:
1. What is the biggest value in this kind of content for the user?
2. What does the user actually want when they save one of these — what
   are they going to look at again in 6 months?
3. What's the best ROI in text form — what should `body_md` look like
   to maximize signal per scroll?
4. Which of the available AFFiNE block flavours best express that?

Available block flavours the generated prompt can request (via markdown):
- Headings h1-h6: `# heading`, `## heading`
- Paragraphs: plain text
- Bulleted/numbered/todo lists: `- item`, `1. item`, `[ ] item`
- Code blocks with language: ```python ... ```  (any language)
- Mermaid diagrams: ```mermaid\\nflowchart ... ```  (renders as diagram)
- Embedded HTML "frames" (SVG charts, styled cards):
  ```embed-html\\n<svg ...> ... ```
- Image refs to available keyframes: `![caption](kf:<index>)`
- Cross-doc references: `[[Doc Title]]` (resolves to embed-linked-doc)
- Callouts (highlighted blocks): `> [!callout] text`
- URL embeds: paste `[](url)`; renderer picks youtube/github/figma/loom
  or falls back to bookmark
- Dividers: `---`

Rules the generated prompt MUST always include:
- Title rule: 1-10 words, English default, Czech/Slovak preserved.
- Lede rule: if source title is a question/mystery/clickbait, populate
  `lede` with one direct answering sentence; else null.
- Summary rule: 3-6 bullets, one short line each, no intro/outro.
- Description rule: mine `extracted.extra.description` for sources,
  citations, related links, chapter markers. Surface them in `body_md`
  (typically `## Sources` section). Strip sponsor/social noise.
- Body rule: tailored to this content type (your design).
- Language rule: English by default; Czech/Slovak preserved if source is.

Return JSON matching the SynthesizedTemplate schema. The `system_prompt`
you generate will be sent to Haiku 4.5 — make it self-contained.
"""


def _build_user_message(
    *, platform_id: str, topic: str, sample: Extracted
) -> str:
    body_excerpt = (sample.body_md or "")[:4000]
    description = (sample.extra or {}).get("description")
    video_summary = (sample.extra or {}).get("video_summary")
    parts: list[str] = [
        f"Platform: {platform_id}",
        f"Topic: {topic}",
        "",
        "Sample capture:",
        f"- Title: {sample.title or '(none)'}",
        f"- Author: {sample.author or '(unknown)'}",
        f"- Media kind: {sample.media_kind.value}",
        "",
    ]
    if description:
        parts += ["Description from publisher:", str(description), ""]
    if video_summary:
        parts += ["Vision-grounded summary:", str(video_summary), ""]
    parts += ["Body excerpt:", body_excerpt]
    return "\n".join(parts)


async def synthesize_template(
    *,
    platform_id: str,
    topic: str,
    sample_extracted: Extracted,
    templates_repo: TemplatesRepository,
) -> ContentTemplate:
    """Run the Sonnet meta-prompt, persist with insert_if_absent, return the row.

    Concurrent calls for the same (platform_id, topic) resolve to a single
    winner via the partial UNIQUE index on `content_templates`.
    """
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    user_msg = _build_user_message(
        platform_id=platform_id, topic=topic, sample=sample_extracted
    )

    response = await client.messages.parse(
        model=settings.vision_model,  # Sonnet 4.6 — runs once per scope
        max_tokens=4096,
        system=[{"type": "text", "text": META_SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": user_msg}],
        output_format=SynthesizedTemplate,
    )

    if response.parsed_output is None:
        raise RuntimeError(
            "template_synth: parsed_output is None — schema-enforced parse failed"
        )

    synth = response.parsed_output
    generator_meta = {
        "biggest_value": synth.biggest_value,
        "user_intent": synth.user_intent,
        "best_roi_format": synth.best_roi_format,
        "available_blocks_used": synth.available_blocks_used,
        "synthesizer_model": settings.vision_model,
    }

    return await templates_repo.insert_if_absent(
        platform_id=platform_id,
        topic=topic,
        name=synth.name,
        system_prompt=synth.system_prompt,
        status="auto",
        created_by="synth",
        generator_meta=generator_meta,
    )
```

- [ ] **Step 5.4: Run test (expected: pass)**

```
cd ingest && python -m pytest tests/test_template_synth.py -v
```

Expected: all 3 tests PASS.

- [ ] **Step 5.5: Commit**

```bash
git add ingest/src/pipeline/template_synth.py ingest/tests/test_template_synth.py
git commit -m "feat(ingest): template_synth.py — Sonnet meta-prompt designs templates"
```

---

## Task 6: markdown_render — AFFiNE block-spec emitter

**Files:**
- Create: `ingest/src/pipeline/markdown_render.py`
- Create: `ingest/tests/test_markdown_render.py`

- [ ] **Step 6.1: Write the failing test**

Create `ingest/tests/test_markdown_render.py`:

```python
"""Tests for markdown → AFFiNE block-spec emitter.

Each block flavour gets its own round-trip test. The MCP client is mocked
for [[Doc Title]] resolution; keyframes are passed as a list of dicts.
"""

from unittest.mock import AsyncMock

import pytest

from src.pipeline.markdown_render import markdown_to_blocks


KEYFRAMES = [
    {"timestamp_seconds": 42.3, "caption": "IDE", "blob_source_id": "blob1"},
    {"timestamp_seconds": 154.0, "caption": "Network", "blob_source_id": "blob2"},
]


@pytest.mark.asyncio
async def test_plain_paragraph():
    blocks = await markdown_to_blocks("Hello world.", keyframes=[], mcp_client=None)
    assert len(blocks) == 1
    assert blocks[0]["type"] == "paragraph"
    assert blocks[0]["style"] == "text"
    assert "Hello world." in str(blocks[0]["text"])


@pytest.mark.asyncio
async def test_heading_levels():
    blocks = await markdown_to_blocks(
        "# H1\n## H2\n### H3\n", keyframes=[], mcp_client=None
    )
    styles = [b["style"] for b in blocks]
    assert styles == ["h1", "h2", "h3"]


@pytest.mark.asyncio
async def test_bulleted_list():
    blocks = await markdown_to_blocks("- a\n- b\n", keyframes=[], mcp_client=None)
    assert len(blocks) == 2
    assert all(b["type"] == "list" and b["style"] == "bulleted" for b in blocks)


@pytest.mark.asyncio
async def test_numbered_list():
    blocks = await markdown_to_blocks("1. a\n2. b\n3. c\n", keyframes=[], mcp_client=None)
    assert len(blocks) == 3
    assert all(b["type"] == "list" and b["style"] == "numbered" for b in blocks)


@pytest.mark.asyncio
async def test_todo_list():
    blocks = await markdown_to_blocks(
        "- [ ] one\n- [x] two\n", keyframes=[], mcp_client=None
    )
    assert len(blocks) == 2
    assert all(b["type"] == "list" and b["style"] == "todo" for b in blocks)
    assert blocks[0].get("checked") is False
    assert blocks[1].get("checked") is True


@pytest.mark.asyncio
async def test_fenced_code_block_with_language():
    md = "```python\nprint('hi')\n```\n"
    blocks = await markdown_to_blocks(md, keyframes=[], mcp_client=None)
    assert len(blocks) == 1
    assert blocks[0]["type"] == "code"
    assert blocks[0]["language"] == "python"
    assert "print('hi')" in blocks[0]["text"]


@pytest.mark.asyncio
async def test_mermaid_renders_as_code_with_language():
    md = "```mermaid\nflowchart TD\n  A --> B\n```\n"
    blocks = await markdown_to_blocks(md, keyframes=[], mcp_client=None)
    assert blocks[0]["type"] == "code"
    assert blocks[0]["language"] == "mermaid"


@pytest.mark.asyncio
async def test_embed_html_sentinel():
    md = "```embed-html\n<svg width='10'/>\n```\n"
    blocks = await markdown_to_blocks(md, keyframes=[], mcp_client=None)
    assert blocks[0]["type"] == "embed-html"
    assert "<svg" in blocks[0]["html"]


@pytest.mark.asyncio
async def test_divider():
    blocks = await markdown_to_blocks("---\n", keyframes=[], mcp_client=None)
    assert blocks[0]["type"] == "divider"


@pytest.mark.asyncio
async def test_callout_syntax():
    blocks = await markdown_to_blocks(
        "> [!callout] Important point.", keyframes=[], mcp_client=None
    )
    assert blocks[0]["type"] == "callout"
    assert "Important point" in str(blocks[0]["text"])


@pytest.mark.asyncio
async def test_keyframe_image_ref_resolves_to_blob_id():
    md = "![the IDE](kf:0)\n"
    blocks = await markdown_to_blocks(md, keyframes=KEYFRAMES, mcp_client=None)
    assert blocks[0]["type"] == "image"
    assert blocks[0]["sourceId"] == "blob1"
    assert blocks[0].get("caption") == "the IDE"


@pytest.mark.asyncio
async def test_keyframe_out_of_range_dropped_silently():
    md = "![nope](kf:99)\nNext paragraph.\n"
    blocks = await markdown_to_blocks(md, keyframes=KEYFRAMES, mcp_client=None)
    # The image block is dropped; the paragraph after it survives.
    assert all(b["type"] != "image" for b in blocks)
    assert any("Next paragraph." in str(b.get("text", "")) for b in blocks)


@pytest.mark.asyncio
async def test_cross_doc_reference_with_match():
    mcp = AsyncMock()
    mcp.find_doc_by_title = AsyncMock(return_value={"matches": [{"id": "doc_abc"}]})
    blocks = await markdown_to_blocks(
        "See [[Phase 13 Plan]] for context.\n",
        keyframes=[],
        mcp_client=mcp,
    )
    # Cross-doc embed appears as its own block; the surrounding text
    # may break into preceding/following paragraphs.
    embeds = [b for b in blocks if b["type"] == "embed-linked-doc"]
    assert len(embeds) == 1
    assert embeds[0]["docId"] == "doc_abc"


@pytest.mark.asyncio
async def test_cross_doc_reference_unresolved_falls_back_to_text():
    mcp = AsyncMock()
    mcp.find_doc_by_title = AsyncMock(return_value={"matches": []})
    blocks = await markdown_to_blocks(
        "See [[Nonexistent Doc]] for context.\n",
        keyframes=[],
        mcp_client=mcp,
    )
    # No embed-linked-doc; the literal text remains.
    assert all(b["type"] != "embed-linked-doc" for b in blocks)
    flat = " ".join(str(b.get("text", "")) for b in blocks)
    assert "[[Nonexistent Doc]]" in flat


@pytest.mark.asyncio
async def test_inline_link_in_paragraph():
    blocks = await markdown_to_blocks(
        "Read [the paper](https://example.com/paper.pdf) now.\n",
        keyframes=[],
        mcp_client=None,
    )
    assert blocks[0]["type"] == "paragraph"
    # text becomes a list of inline ops when there's any rich formatting.
    ops = blocks[0]["text"]
    assert isinstance(ops, list)
    linked = [op for op in ops if isinstance(op, dict) and op.get("link")]
    assert len(linked) == 1
    assert linked[0]["link"] == "https://example.com/paper.pdf"


@pytest.mark.asyncio
async def test_url_embed_with_empty_label_promotes_to_embed():
    """`[](https://www.youtube.com/watch?v=X)` (no label) → embed-youtube block."""
    blocks = await markdown_to_blocks(
        "[](https://www.youtube.com/watch?v=dQw4w9WgXcQ)\n",
        keyframes=[],
        mcp_client=None,
    )
    assert blocks[0]["type"] == "embed-youtube"
```

- [ ] **Step 6.2: Run test (expected: fail — module missing)**

```
cd ingest && python -m pytest tests/test_markdown_render.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 6.3: Implement `markdown_render.py`**

Create `ingest/src/pipeline/markdown_render.py`:

```python
"""Markdown → AFFiNE block-spec emitter.

Uses `markdown-it-py` for CommonMark parsing. Adds project-specific
syntax handled inline:
  - Fenced code blocks with language sentinel `embed-html` → embed-html block
  - Image refs `![alt](kf:<n>)` → image block backed by keyframe blob_source_id
  - Cross-doc refs `[[Doc Title]]` → embed-linked-doc block (async MCP call)
  - Callout blocks `> [!callout] text` → affine:callout block

Async because cross-doc resolution hits the MCP server.

Returns a list of block-spec dicts in the shape consumed by
mcp_ext's append_blocks tool (see [`mcp-ext/src/write-tools.ts`]).
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

from markdown_it import MarkdownIt
from markdown_it.token import Token

log = logging.getLogger(__name__)

_KF_REF_RE = re.compile(r"^kf:(\d+)$")
_CALLOUT_RE = re.compile(r"^\s*>\s*\[!callout\]\s*(.*)$", re.MULTILINE)
_CROSS_DOC_RE = re.compile(r"\[\[([^\]]+)\]\]")
_INLINE_LINK_RE = re.compile(
    r"\[(?P<label>[^\]]*)\]\((?P<url>[^)\s]+)\)"
)


async def markdown_to_blocks(
    md: str,
    *,
    keyframes: list[dict[str, Any]],
    mcp_client: Any | None,
) -> list[dict[str, Any]]:
    """Parse `md` and emit AFFiNE block specs.

    `keyframes` resolves `kf:<n>` image refs to image blocks with the
    n-th keyframe's `blob_source_id` and caption.
    `mcp_client` resolves `[[Doc Title]]` refs via `find_doc_by_title`.
    Pass None to skip cross-doc resolution (refs render as plain text).
    """
    # Pre-pass: convert callout lines to a sentinel that the token stream can pick up.
    md = _CALLOUT_RE.sub(r":::callout\n\1\n:::", md)

    parser = MarkdownIt("commonmark", {"breaks": False, "html": False})
    tokens = parser.parse(md)

    blocks: list[dict[str, Any]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]

        # Headings
        if tok.type == "heading_open":
            level = int(tok.tag[1])  # h1..h6 → 1..6
            inline = tokens[i + 1]
            blocks.append({
                "type": "paragraph",
                "style": f"h{level}",
                "text": _inline_to_text(inline.content),
            })
            i += 3  # heading_open, inline, heading_close
            continue

        # Horizontal rule → divider
        if tok.type == "hr":
            blocks.append({"type": "divider"})
            i += 1
            continue

        # Fenced code block
        if tok.type == "fence":
            lang = (tok.info or "").strip()
            if lang == "embed-html":
                blocks.append({"type": "embed-html", "html": tok.content})
            else:
                blocks.append({
                    "type": "code",
                    "language": lang or "text",
                    "text": tok.content.rstrip("\n"),
                })
            i += 1
            continue

        # Callout sentinel block (from pre-pass `:::callout ... :::`)
        if tok.type == "container_callout_open":
            # markdown-it doesn't natively parse `:::` — see _maybe_handle_sentinel
            i += 1
            continue

        # Bulleted / numbered / todo list
        if tok.type in ("bullet_list_open", "ordered_list_open"):
            style = "bulleted" if tok.type == "bullet_list_open" else "numbered"
            i += 1
            while i < len(tokens) and tokens[i].type != f"{tok.type[:-5]}_close":
                if tokens[i].type == "list_item_open":
                    # Collect the item's content
                    item_inline = _find_first_inline_after(tokens, i)
                    item_text = item_inline.content if item_inline else ""
                    item_block = _maybe_todo_block(item_text, style)
                    if item_block is None:
                        item_block = {
                            "type": "list",
                            "style": style,
                            "text": _inline_to_text(item_text),
                        }
                    blocks.append(item_block)
                i += 1
            i += 1  # consume closing token
            continue

        # Paragraph (default)
        if tok.type == "paragraph_open":
            inline = tokens[i + 1]
            text = inline.content
            # Sentinel handling for the pre-pass callout container:
            if text.startswith(":::callout\n") or text.startswith(":::callout "):
                # not happening — pre-pass already line-split
                pass
            # Standalone empty-label URL → embed
            embed = _try_url_embed(text)
            if embed is not None:
                blocks.append(embed)
                i += 3
                continue
            # Cross-doc refs may split a paragraph into multiple blocks.
            new_blocks = await _split_on_cross_doc_refs(text, mcp_client)
            # Replace image refs (![alt](kf:N)) — these become standalone image blocks.
            new_blocks = _replace_keyframe_refs(new_blocks, keyframes)
            blocks.extend(new_blocks)
            i += 3  # paragraph_open, inline, paragraph_close
            continue

        # Callout: produced by the pre-pass via a literal `:::callout\n<text>\n:::` paragraph.
        # The CommonMark parser will treat that as plain paragraph text; we detect it here.
        if tok.type == "inline":
            # already handled inside paragraph_open above; skip stray inlines.
            i += 1
            continue

        # Quote block (not callout — already pre-transformed)
        if tok.type == "blockquote_open":
            close_idx = _find_matching_close(tokens, i, "blockquote_open", "blockquote_close")
            # Concatenate inline content within the quote.
            inner_text = " ".join(
                t.content for t in tokens[i + 1:close_idx] if t.type == "inline"
            )
            blocks.append({
                "type": "paragraph",
                "style": "quote",
                "text": _inline_to_text(inner_text),
            })
            i = close_idx + 1
            continue

        i += 1

    # Post-pass: detect any leftover `:::callout` pseudo-blocks emitted as
    # plain paragraphs. (markdown-it doesn't parse fence-style containers by
    # default; the pre-pass replaces callout lines with `:::callout\n…\n:::`
    # blocks which appear as a single multi-line paragraph.)
    return _convert_callout_pseudoblocks(blocks)


def _convert_callout_pseudoblocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for b in blocks:
        text = b.get("text")
        flat = text if isinstance(text, str) else (
            " ".join(op.get("text", "") for op in text) if isinstance(text, list) else ""
        )
        if b.get("type") == "paragraph" and flat.startswith(":::callout"):
            # strip `:::callout\n` and trailing `:::`
            body = flat[len(":::callout"):].strip().rstrip(":").strip()
            out.append({"type": "callout", "text": body})
            continue
        out.append(b)
    return out


def _maybe_todo_block(item_text: str, parent_style: str) -> dict[str, Any] | None:
    """Detect GFM task-list items `[ ]` / `[x]` at the start of a list item."""
    t = item_text.lstrip()
    if t.startswith("[ ] "):
        return {"type": "list", "style": "todo", "checked": False, "text": t[4:]}
    if t.startswith("[x] ") or t.startswith("[X] "):
        return {"type": "list", "style": "todo", "checked": True, "text": t[4:]}
    return None


def _find_first_inline_after(tokens: list[Token], start: int) -> Token | None:
    for t in tokens[start + 1:]:
        if t.type == "inline":
            return t
        if t.type == "list_item_close":
            break
    return None


def _find_matching_close(
    tokens: list[Token], start: int, open_type: str, close_type: str
) -> int:
    depth = 1
    i = start + 1
    while i < len(tokens):
        if tokens[i].type == open_type:
            depth += 1
        elif tokens[i].type == close_type:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(tokens) - 1


def _try_url_embed(text: str) -> dict[str, Any] | None:
    """A paragraph that contains ONLY `[](url)` becomes a URL embed."""
    m = _INLINE_LINK_RE.fullmatch(text.strip())
    if m is None or m.group("label"):
        return None
    url = m.group("url")
    host = (urlparse(url).hostname or "").lower()
    if host in ("youtu.be",) or host == "youtube.com" or host.endswith(".youtube.com"):
        return {"type": "embed-youtube", "url": url}
    if host == "github.com" or host.endswith(".github.com"):
        return {"type": "embed-github", "url": url}
    if host == "figma.com" or host.endswith(".figma.com"):
        return {"type": "embed-figma", "url": url}
    if host == "loom.com" or host.endswith(".loom.com"):
        return {"type": "embed-loom", "url": url}
    return {"type": "bookmark", "url": url}


def _inline_to_text(text: str):
    """Parse `[label](url)` inline links into the inline-op list shape
    that mcp-ext's block-builder converts to rich-text deltas."""
    if "](" not in text:
        return text
    parts: list[dict[str, Any]] = []
    pos = 0
    for m in _INLINE_LINK_RE.finditer(text):
        if m.start() > pos:
            parts.append({"text": text[pos:m.start()]})
        label = m.group("label") or m.group("url")
        parts.append({"text": label, "link": m.group("url")})
        pos = m.end()
    if pos < len(text):
        parts.append({"text": text[pos:]})
    return parts if parts else text


async def _split_on_cross_doc_refs(
    text: str, mcp_client: Any | None
) -> list[dict[str, Any]]:
    """Split a paragraph on `[[Doc Title]]` refs. Each ref becomes its own
    embed-linked-doc block; the surrounding text becomes adjacent paragraphs.
    Unresolved refs stay inline as literal text."""
    matches = list(_CROSS_DOC_RE.finditer(text))
    if not matches:
        return [{"type": "paragraph", "style": "text", "text": _inline_to_text(text)}]

    out: list[dict[str, Any]] = []
    pos = 0
    for m in matches:
        if m.start() > pos:
            pre = text[pos:m.start()]
            out.append({"type": "paragraph", "style": "text", "text": _inline_to_text(pre)})

        title = m.group(1)
        doc_id = None
        if mcp_client is not None:
            try:
                resp = await mcp_client.find_doc_by_title(title)
                matches_resp = resp.get("matches") if isinstance(resp, dict) else None
                if matches_resp and len(matches_resp) == 1:
                    doc_id = matches_resp[0].get("id")
            except Exception as e:  # noqa: BLE001
                log.warning("find_doc_by_title failed for %r: %s", title, e)

        if doc_id is not None:
            out.append({"type": "embed-linked-doc", "docId": doc_id})
        else:
            # Unresolved: keep the literal `[[Title]]` inline as paragraph text.
            out.append({"type": "paragraph", "style": "text",
                        "text": _inline_to_text(f"[[{title}]]")})

        pos = m.end()
    if pos < len(text):
        out.append({"type": "paragraph", "style": "text",
                    "text": _inline_to_text(text[pos:])})
    return out


_IMAGE_REF_RE = re.compile(r"^!\[(?P<alt>[^\]]*)\]\((?P<src>[^)\s]+)\)\s*$")


def _replace_keyframe_refs(
    blocks: list[dict[str, Any]], keyframes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Walk paragraph blocks; if a paragraph is exactly an image ref
    `![alt](kf:N)`, replace with an image block backed by the keyframe."""
    out: list[dict[str, Any]] = []
    for b in blocks:
        if b.get("type") != "paragraph":
            out.append(b)
            continue
        flat = b.get("text")
        if not isinstance(flat, str):
            out.append(b)
            continue
        m = _IMAGE_REF_RE.match(flat.strip())
        if m is None:
            out.append(b)
            continue
        src = m.group("src")
        kfm = _KF_REF_RE.match(src)
        if kfm is None:
            # External image — out of scope v1; drop the line, log.
            log.warning("external image ref dropped: %s", src)
            continue
        idx = int(kfm.group(1))
        if idx < 0 or idx >= len(keyframes):
            log.warning("keyframe ref kf:%d out of range (0..%d)", idx, len(keyframes) - 1)
            continue
        kf = keyframes[idx]
        sid = kf.get("blob_source_id")
        if not sid:
            log.warning("keyframe kf:%d missing blob_source_id", idx)
            continue
        out.append({
            "type": "image",
            "sourceId": sid,
            "caption": m.group("alt") or kf.get("caption", ""),
        })
    return out
```

- [ ] **Step 6.4: Run test (expected: pass)**

```
cd ingest && python -m pytest tests/test_markdown_render.py -v
```

Expected: all 16 tests PASS. If markdown-it-py isn't installed yet:
```
pip install -e .[dev]
```

- [ ] **Step 6.5: Commit**

```bash
git add ingest/src/pipeline/markdown_render.py ingest/tests/test_markdown_render.py
git commit -m "feat(ingest): markdown_render.py — AFFiNE block emitter with rich palette"
```

---

## Task 7: Orchestrator integration + delete summarizer

**Files:**
- Modify: `ingest/src/pipeline/orchestrator.py`
- Modify: `ingest/src/db.py` — add `save_template_run` + `save_extracted_snapshot`
- Delete: `ingest/src/pipeline/summarizer.py`
- Delete: `ingest/tests/test_summarizer.py`
- Modify: `ingest/tests/test_orchestrator.py` (extend existing tests)

- [ ] **Step 7.1: Add DB methods for template-run persistence**

Append to `CaptureRepository` in `ingest/src/db.py` (after `mark_done`):

```python
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
        can replay against the same inputs without re-fetching the source."""
        import json
        await self._conn.execute(
            """
            UPDATE captures
            SET extracted_snapshot = $2::jsonb, updated_at = NOW()
            WHERE id = $1
            """,
            capture_id, json.dumps(snapshot),
        )
```

- [ ] **Step 7.2: Write failing test for orchestrator integration**

Extend `ingest/tests/test_orchestrator.py` (find the file; if it has fixtures like `_fake_filer` reuse them). Append:

```python
"""Phase 14 — template-driven orchestrator integration."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.pipeline.extracted import Extracted, MediaKind
from src.pipeline.templates import ContentTemplate
from src.pipeline.templated_render import TemplatedOutput


def _content_template(**overrides) -> ContentTemplate:
    from datetime import datetime, timezone
    base = dict(
        id="t_seed", platform_id="*", topic="*",
        name="Default summarizer", system_prompt="default prompt",
        status="auto", generator_meta=None, created_by="synth",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return ContentTemplate(**base)


@pytest.mark.asyncio
async def test_orchestrator_uses_resolved_template():
    """Resolve returns a template → orchestrator uses it, no synthesis."""
    from src.pipeline.orchestrator import process_capture
    # Build mocks for: row, platform, topics, repo, filer, extract_fn,
    # classify_fn, summarize_fn=None, templates_repo, render_fn, synth_fn
    # (Existing test_orchestrator.py has helpers — reuse them.)
    # Assertion: templates_repo.resolve called with the classifier's topic,
    # synthesize_template NOT called, render_fn called with the resolved
    # template, capture row's template_id matches the resolved id.
    # (Test implementation follows existing test_orchestrator.py patterns.)
    pass  # SKELETON — implement using existing test helpers.


@pytest.mark.asyncio
async def test_orchestrator_synthesizes_template_when_resolve_returns_none():
    """No template anywhere → synthesize then use."""
    pass  # SKELETON — implement using existing test helpers.


@pytest.mark.asyncio
async def test_orchestrator_persists_template_run():
    """After successful render, repo.save_template_run called with the
    snapshot of system_prompt and body_md."""
    pass  # SKELETON — implement using existing test helpers.


@pytest.mark.asyncio
async def test_orchestrator_renders_lede_as_callout_block():
    """When TemplatedOutput.lede is set, the rendered blocks include
    a `callout` block right after the URL embed and before the Summary."""
    pass  # SKELETON — implement using existing test helpers.


@pytest.mark.asyncio
async def test_orchestrator_drops_hardcoded_keyframes_section():
    """The legacy `## Keyframes` block dump is gone. Keyframes only appear
    where the template references them via `kf:N`."""
    pass  # SKELETON — implement using existing test helpers.
```

> **Note for the engineer:** the existing `test_orchestrator.py` already has fixtures and pattern conventions (mocking `Filer`, `extract_fn`, `classify_fn`, asserting `repo.mark_*` calls). Implement the bodies of these tests using those existing patterns. The skeletons above define the assertions; fill in the mock plumbing the same way the file already does.

- [ ] **Step 7.3: Replace summarizer call in orchestrator**

In [`ingest/src/pipeline/orchestrator.py`](../../ingest/src/pipeline/orchestrator.py):

Replace these imports:
```python
from src.pipeline.summarizer import SummaryResult, fallback_title, summarize
```
with:
```python
from src.pipeline.markdown_render import markdown_to_blocks
from src.pipeline.templated_render import TemplatedOutput, fallback_title, render as templated_render
from src.pipeline.template_synth import synthesize_template
from src.pipeline.templates import TemplatesRepository
```

Update the `SummarizeFunc` typedef + the `process_capture` signature:

```python
RenderFunc = Callable[..., Awaitable[TemplatedOutput]]
SynthFunc = Callable[..., Awaitable[Any]]  # returns ContentTemplate


async def process_capture(
    row: CaptureRow,
    *,
    platform: Platform,
    topics: TopicsConfig,
    repo: CaptureRepository,
    filer: Filer,
    extract_fn: ExtractFunc,
    classify_fn: ClassifyFunc,
    templates_repo: TemplatesRepository,
    render_fn: RenderFunc | None = None,
    synth_fn: SynthFunc | None = None,
) -> None:
    render_fn = render_fn or templated_render
    synth_fn = synth_fn or synthesize_template
    # ... (existing extract step unchanged)
```

Inside `process_capture`, after classification, **replace** the existing summarize block (the section from line ~78 starting `# ── Summarize (best-effort) ...` through `if folder_id is not None: await filer._mcp.move_document(...)`):

```python
    # ── Resolve or synthesize template ─────────────────────────────────
    template = await templates_repo.resolve(
        platform_id=platform.id, topic=result.topic or "*",
    )
    if template is None:
        try:
            template = await synth_fn(
                platform_id=platform.id,
                topic=result.topic or "*",
                sample_extracted=extracted,
                templates_repo=templates_repo,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("template synthesis failed; falling back to (*, *): %s", e)
            template = await templates_repo.resolve(platform_id="*", topic="*")
            if template is None:
                raise RuntimeError(
                    "No (*, *) seed template exists and synthesis failed — "
                    "cannot render this capture."
                ) from e

    # ── Snapshot inputs for replay (rerender endpoint) ─────────────────
    await repo.save_extracted_snapshot(
        capture_id=row.id,
        snapshot=_extracted_to_dict(extracted),
    )

    # ── Templated render ───────────────────────────────────────────────
    keyframes = (extracted.extra or {}).get("keyframes") or []
    try:
        rendered = await render_fn(template=template, extracted=extracted, keyframes=keyframes)
    except Exception as e:  # noqa: BLE001
        log.warning("templated render failed: %s", e)
        rendered = None

    if rendered is not None:
        new_title = (rendered.title or "").strip() or fallback_title(extracted, url=row.url)
        await repo.save_template_run(
            capture_id=row.id,
            template_id=template.id,
            prompt_used=template.system_prompt,
            output_raw=rendered.body_md,
        )
    else:
        new_title = fallback_title(extracted, url=row.url)

    try:
        await filer._mcp.set_doc_title(row.doc_id, new_title)
        log.info("transition", extra={"step": "titled", "title": new_title})
    except Exception as e:  # noqa: BLE001
        log.warning("set_doc_title failed (continuing): %s", e)

    # ── File (move + append body) ────────────────────────────────────
    platform_path = ["Sources", platform.group, platform.folder_name]
    folder_id = await filer.move_to_topic_folder(
        platform_path=platform_path, result=result,
    )

    if folder_id is not None:
        topic_path = "/".join(platform_path + [result.topic or ""])
    else:
        topic_path = "/".join(platform_path)

    await repo.mark_filing(capture_id=row.id, topic_path=topic_path)
    log.info("transition", extra={"step": "filed", "topic_path": topic_path})

    if folder_id is not None:
        await filer._mcp.move_document(row.doc_id, folder_id=folder_id)

    # ── Render the doc body via the rich block emitter ──────────────────
    await _replace_doc_body_templated(
        filer=filer,
        doc_id=row.doc_id,
        rendered=rendered,
        keyframes=keyframes,
        url=row.url,
    )

    await repo.mark_done(row.id)
    log.info("transition", extra={"step": "done"})


def _extracted_to_dict(extracted: Extracted) -> dict:
    """Serialize an Extracted record to a JSON-able dict for snapshotting."""
    return {
        "title": extracted.title,
        "body_md": extracted.body_md,
        "author": extracted.author,
        "published_at": extracted.published_at.isoformat() if extracted.published_at else None,
        "media_kind": extracted.media_kind.value,
        "extra": extracted.extra,
    }


async def _replace_doc_body_templated(
    *,
    filer,
    doc_id: str,
    rendered: TemplatedOutput | None,
    keyframes: list[dict[str, Any]],
    url: str | None,
) -> None:
    """Delete the stub block and append the templated layout:
        [embed url]
        [callout: lede]    (when lede != None)
        ## Summary
        - bullets
        <body_md tree>
        Source: <url>
    """
    try:
        await _delete_stub_block(filer=filer, doc_id=doc_id)
    except Exception as e:  # noqa: BLE001
        log.warning("stub block cleanup failed (continuing): %s", e)

    blocks: list[dict[str, Any]] = []
    if url:
        blocks.append(_url_embed_block(url))

    if rendered is not None:
        if rendered.lede:
            blocks.append({"type": "callout", "text": rendered.lede})
        if rendered.summary_md:
            blocks.append({"type": "paragraph", "style": "h2", "text": "Summary"})
            blocks.extend(
                await markdown_to_blocks(rendered.summary_md, keyframes=keyframes, mcp_client=filer._mcp)
            )
        if rendered.body_md:
            blocks.extend(
                await markdown_to_blocks(rendered.body_md, keyframes=keyframes, mcp_client=filer._mcp)
            )

    if url:
        blocks.append({
            "type": "paragraph",
            "style": "text",
            "text": [{"text": "Source: "}, {"text": url, "italic": True, "link": url}],
        })

    if not blocks:
        blocks.append({"type": "paragraph", "style": "text", "text": "(no rendered content)"})

    await filer._mcp.append_blocks(doc_id, blocks)
```

Delete from `orchestrator.py` (now unused):
- `_try_summarize()`
- `_replace_doc_body()` (old version)
- `_build_body_blocks()`
- `_markdown_to_blocks()`
- `_HEADING_PREFIXES`, `_INLINE_LINK_RE`, `_parse_inline_markdown`
- `SummarizeFunc` typedef

Keep:
- `_url_embed_block()`
- `_delete_stub_block()`
- `_list_existing_siblings()`
- `_media_kind_for_text()`

- [ ] **Step 7.4: Delete summarizer.py + its tests**

```bash
rm ingest/src/pipeline/summarizer.py
rm ingest/tests/test_summarizer.py
```

- [ ] **Step 7.5: Update `_process_fn` in api.py to pass `templates_repo`**

In [`ingest/src/api.py:117-127`](../../ingest/src/api.py), the existing `_process_fn` calls `process_capture` without the new required `templates_repo` kwarg. Update:

```python
        async def _process_fn(row, **kwargs):
            extractor = get_extractor(kwargs["platform"].extractor)
            await process_capture(
                row,
                platform=kwargs["platform"],
                topics=kwargs["topics"],
                repo=kwargs["repo"],
                filer=app_state.filer,
                extract_fn=extractor,
                classify_fn=classify,
                templates_repo=app_state.templates_repo,  # ← NEW
            )
```

If `app_state.templates_repo` is None (pool didn't initialize for any reason), the orchestrator will hit `await templates_repo.resolve(...)` with None and `AttributeError`. Guard it: if `templates_repo` is None at the call site, raise a clear error rather than booting the worker — same pattern as the existing `AFFINE_ACCESS_TOKEN` guard at lifespan startup.

- [ ] **Step 7.6: Implement skeleton orchestrator tests**

Open [`ingest/tests/test_orchestrator.py`](../../ingest/tests/test_orchestrator.py), find the existing test helpers (look for `_fake_filer`, `_fake_repo`, `_fake_classify` patterns). Implement the 5 SKELETON tests added in Step 7.2 using those patterns.

For each, the pattern is:
```python
@pytest.mark.asyncio
async def test_orchestrator_uses_resolved_template():
    row = _capture_row()
    platform = _platform()
    topics = _topics()
    repo = _fake_repo()
    filer = _fake_filer()
    extract_fn = AsyncMock(return_value=_extracted_record())
    classify_fn = AsyncMock(return_value=_classification_result(topic="Tutorials"))

    templates_repo = AsyncMock()
    templates_repo.resolve = AsyncMock(return_value=_content_template(
        id="t_yt_tut", platform_id="youtube", topic="Tutorials"))

    synth_fn = AsyncMock()  # must NOT be called
    render_fn = AsyncMock(return_value=TemplatedOutput(
        title="T", lede=None, summary_md="- a", body_md="b"))

    from src.pipeline.orchestrator import process_capture
    await process_capture(
        row, platform=platform, topics=topics, repo=repo, filer=filer,
        extract_fn=extract_fn, classify_fn=classify_fn,
        templates_repo=templates_repo, render_fn=render_fn, synth_fn=synth_fn,
    )

    templates_repo.resolve.assert_awaited_once()
    synth_fn.assert_not_awaited()
    render_fn.assert_awaited_once()
    repo.save_template_run.assert_awaited_once()
    args = repo.save_template_run.await_args.kwargs
    assert args["template_id"] == "t_yt_tut"
```

For the other 4 SKELETONs: similar shape, varying which mock returns what (resolve → None forces synth_fn; lede populated checks for callout in `filer._mcp.append_blocks` payload; keyframes check that the keyframes list passes through to render_fn but the orchestrator doesn't append a `## Keyframes` heading itself).

- [ ] **Step 7.7: Run test (expected: pass)**

```
cd ingest && python -m pytest tests/test_orchestrator.py -v
```

Expected: existing orchestrator tests still pass (after their call sites are updated to pass `templates_repo`); the 5 new ones pass.

- [ ] **Step 7.8: Run the full ingest test suite to confirm no regressions**

```
cd ingest && python -m pytest -x
```

Expected: PASS (excluding the deleted `test_summarizer.py`).

- [ ] **Step 7.9: Commit**

```bash
git add -A ingest/
git commit -m "feat(ingest): orchestrator uses content templates; remove summarizer.py"
```

---

## Task 8: API endpoints — templates CRUD + ops

**Files:**
- Modify: `ingest/src/models.py` — add wire models
- Modify: `ingest/src/api.py` — add endpoints
- Create: `ingest/tests/test_template_api.py`

- [ ] **Step 8.1: Add Pydantic wire models**

Append to `ingest/src/models.py`:

```python
# ── Templates (Phase 14) ─────────────────────────────────────────────


class ContentTemplateView(BaseModel):
    """API response shape for a template row."""

    id: str
    platform_id: str
    topic: str
    name: str
    system_prompt: str
    status: str
    generator_meta: dict | None = None
    created_by: str
    created_at: datetime
    updated_at: datetime
    usage_count: int = 0


class CreateTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform_id: str = Field(..., min_length=1, max_length=64)
    topic: str = Field(..., min_length=1, max_length=128)
    name: str = Field(..., min_length=1, max_length=128)
    system_prompt: str = Field(..., min_length=1)


class UpdateTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform_id: str | None = Field(default=None, min_length=1, max_length=64)
    topic: str | None = Field(default=None, min_length=1, max_length=128)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    system_prompt: str | None = Field(default=None, min_length=1)


class SynthesizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform_id: str = Field(..., min_length=1, max_length=64)
    topic: str = Field(..., min_length=1, max_length=128)
    sample_capture_id: str | None = None
```

- [ ] **Step 8.2: Write the failing test**

Create `ingest/tests/test_template_api.py`:

```python
"""Tests for /templates/* and /captures/{id}/rerender endpoints.

Uses FastAPI's TestClient with a mocked TemplatesRepository on app_state.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api import app, app_state
from src.pipeline.templates import ContentTemplate


def _tmpl(**ov) -> ContentTemplate:
    base = dict(
        id="t1", platform_id="youtube", topic="Tutorials",
        name="YouTube Tutorial v1", system_prompt="prompt",
        status="edited", generator_meta=None, created_by="user",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    base.update(ov)
    return ContentTemplate(**base)


@pytest.fixture
def client(monkeypatch):
    """Inject a mocked TemplatesRepository into app_state."""
    repo = AsyncMock()
    monkeypatch.setattr(app_state, "templates_repo", repo, raising=False)
    monkeypatch.setenv("INGEST_API_TOKEN", "test-token")
    with TestClient(app) as c:
        yield c, repo


HEADERS = {"Authorization": "Bearer test-token"}


def test_get_templates_returns_list(client):
    c, repo = client
    repo.list_all = AsyncMock(return_value=[_tmpl(), _tmpl(id="t2", platform_id="*", topic="*")])
    repo.count_usage = AsyncMock(return_value=5)

    r = c.get("/templates", headers=HEADERS)

    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert body[0]["usage_count"] == 5


def test_get_templates_with_filters(client):
    c, repo = client
    repo.list_all = AsyncMock(return_value=[])
    repo.count_usage = AsyncMock(return_value=0)

    r = c.get("/templates?platform=youtube&status=edited", headers=HEADERS)

    assert r.status_code == 200
    repo.list_all.assert_awaited_once()
    kwargs = repo.list_all.await_args.kwargs
    assert kwargs["platform_id"] == "youtube"
    assert kwargs["status"] == "edited"


def test_get_template_by_id(client):
    c, repo = client
    repo.get = AsyncMock(return_value=_tmpl())
    repo.count_usage = AsyncMock(return_value=3)

    r = c.get("/templates/t1", headers=HEADERS)

    assert r.status_code == 200
    assert r.json()["id"] == "t1"
    assert r.json()["usage_count"] == 3


def test_get_template_404(client):
    c, repo = client
    repo.get = AsyncMock(return_value=None)
    r = c.get("/templates/nope", headers=HEADERS)
    assert r.status_code == 404


def test_post_template_creates(client):
    c, repo = client
    repo.create = AsyncMock(return_value=_tmpl())
    repo.count_usage = AsyncMock(return_value=0)

    r = c.post("/templates", headers=HEADERS, json={
        "platform_id": "youtube", "topic": "Tutorials",
        "name": "YouTube Tutorial v1", "system_prompt": "prompt",
    })

    assert r.status_code == 201
    assert r.json()["id"] == "t1"


def test_post_template_409_on_conflict(client):
    c, repo = client
    import asyncpg
    repo.create = AsyncMock(side_effect=asyncpg.UniqueViolationError("dup"))

    r = c.post("/templates", headers=HEADERS, json={
        "platform_id": "youtube", "topic": "Tutorials",
        "name": "x", "system_prompt": "x",
    })

    assert r.status_code == 409


def test_put_template_updates(client):
    c, repo = client
    repo.update = AsyncMock(return_value=_tmpl(name="new name"))
    repo.count_usage = AsyncMock(return_value=0)

    r = c.put("/templates/t1", headers=HEADERS, json={"name": "new name"})

    assert r.status_code == 200
    assert r.json()["name"] == "new name"


def test_put_template_404(client):
    c, repo = client
    repo.update = AsyncMock(return_value=None)
    r = c.put("/templates/nope", headers=HEADERS, json={"name": "x"})
    assert r.status_code == 404


def test_delete_template_archives(client):
    c, repo = client
    archived = _tmpl(status="archived")
    repo.get = AsyncMock(return_value=_tmpl())
    repo.archive = AsyncMock(return_value=archived)
    repo.list_all = AsyncMock(return_value=[])  # used for seed-protection check
    repo.count_usage = AsyncMock(return_value=0)

    r = c.delete("/templates/t1", headers=HEADERS)

    assert r.status_code == 200
    assert r.json()["status"] == "archived"


def test_delete_seed_default_rejected_when_last_one(client):
    c, repo = client
    seed = _tmpl(id="seed", platform_id="*", topic="*", status="auto")
    repo.get = AsyncMock(return_value=seed)
    repo.list_all = AsyncMock(return_value=[seed])  # ONLY (*, *) row → reject

    r = c.delete("/templates/seed", headers=HEADERS)

    assert r.status_code == 409
    assert "seed" in r.text.lower() or "default" in r.text.lower()


def test_resolve_returns_template(client):
    c, repo = client
    repo.resolve = AsyncMock(return_value=_tmpl())
    repo.count_usage = AsyncMock(return_value=0)

    r = c.get("/templates/resolve?platform=youtube&topic=Tutorials", headers=HEADERS)

    assert r.status_code == 200
    assert r.json()["id"] == "t1"


def test_resolve_404_when_no_match(client):
    c, repo = client
    repo.resolve = AsyncMock(return_value=None)
    r = c.get("/templates/resolve?platform=x&topic=y", headers=HEADERS)
    assert r.status_code == 404


def test_synthesize_creates_new_template(client, monkeypatch):
    c, repo = client
    repo.resolve = AsyncMock(return_value=None)
    repo.count_usage = AsyncMock(return_value=0)

    # Mock the synthesizer function
    new_tmpl = _tmpl(id="t_new", platform_id="youtube", topic="Recipes", status="auto")
    fake_synth = AsyncMock(return_value=new_tmpl)
    monkeypatch.setattr("src.api.synthesize_template", fake_synth, raising=False)

    # Mock a capture row with an extracted snapshot.
    repo_captures = MagicMock()
    repo_captures.get_by_id = AsyncMock(return_value=MagicMock(
        url="https://example.com",
        extracted_snapshot={"title": "T", "body_md": "B",
                            "author": None, "media_kind": "video", "extra": {}},
    ))
    monkeypatch.setattr(app_state, "captures_repo_factory",
                        lambda conn: repo_captures, raising=False)

    r = c.post("/templates/synthesize", headers=HEADERS, json={
        "platform_id": "youtube", "topic": "Recipes",
    })

    assert r.status_code == 201
    assert r.json()["id"] == "t_new"


def test_synthesize_409_when_active_template_exists(client):
    c, repo = client
    repo.resolve = AsyncMock(return_value=_tmpl())

    r = c.post("/templates/synthesize", headers=HEADERS, json={
        "platform_id": "youtube", "topic": "Tutorials",
    })

    assert r.status_code == 409
```

- [ ] **Step 8.3: Run test (expected: fail — endpoints missing)**

```
cd ingest && python -m pytest tests/test_template_api.py -v
```

Expected: all FAIL with 404 (endpoints not registered yet).

- [ ] **Step 8.4: Implement endpoints in api.py**

Add to [`ingest/src/api.py`](../../ingest/src/api.py). First, augment the lifespan to construct a `TemplatesRepository`:

In `class AppState:` add:
```python
    templates_repo: object | None = None
```

In `lifespan` after pool is created:
```python
    if app_state.pool is not None:
        from src.pipeline.templates import TemplatesRepository
        # Repo is conn-bound; for handlers we'll acquire from pool per request.
        # Stash the pool reference; create per-call repos.
        app_state.templates_repo = TemplatesRepository(app_state.pool)
```

Add imports near the top:
```python
from src.models import (
    ContentTemplateView,
    CreateTemplateRequest,
    UpdateTemplateRequest,
    SynthesizeRequest,
)
from src.pipeline.templates import ContentTemplate, TemplatesRepository
from src.pipeline.template_synth import synthesize_template
```

Add endpoints (before the YouTube cookie endpoints):

```python
def _template_to_view(t: ContentTemplate, usage_count: int) -> ContentTemplateView:
    return ContentTemplateView(
        id=t.id, platform_id=t.platform_id, topic=t.topic,
        name=t.name, system_prompt=t.system_prompt, status=t.status,
        generator_meta=t.generator_meta, created_by=t.created_by,
        created_at=t.created_at, updated_at=t.updated_at,
        usage_count=usage_count,
    )


def _require_templates_repo() -> TemplatesRepository:
    repo = app_state.templates_repo
    if repo is None:
        raise HTTPException(status_code=503, detail="templates_repo not initialized")
    return repo


@app.get("/templates", response_model=list[ContentTemplateView])
async def list_templates(
    platform: str | None = None,
    topic: str | None = None,
    status_filter: str | None = None,
    _: bool = Depends(require_token),
):
    repo = _require_templates_repo()
    rows = await repo.list_all(platform_id=platform, topic=topic, status=status_filter)
    return [_template_to_view(t, await repo.count_usage(template_id=t.id)) for t in rows]


@app.get("/templates/resolve", response_model=ContentTemplateView)
async def resolve_template(
    platform: str, topic: str, _: bool = Depends(require_token),
):
    repo = _require_templates_repo()
    t = await repo.resolve(platform_id=platform, topic=topic)
    if t is None:
        raise HTTPException(status_code=404, detail="no template matches")
    return _template_to_view(t, await repo.count_usage(template_id=t.id))


@app.get("/templates/{template_id}", response_model=ContentTemplateView)
async def get_template(template_id: str, _: bool = Depends(require_token)):
    repo = _require_templates_repo()
    t = await repo.get(template_id=template_id)
    if t is None:
        raise HTTPException(status_code=404)
    return _template_to_view(t, await repo.count_usage(template_id=t.id))


@app.post("/templates", response_model=ContentTemplateView, status_code=201)
async def create_template(
    body: CreateTemplateRequest, _: bool = Depends(require_token),
):
    repo = _require_templates_repo()
    try:
        t = await repo.create(
            platform_id=body.platform_id,
            topic=body.topic,
            name=body.name,
            system_prompt=body.system_prompt,
            status="edited",
            created_by="user",
            generator_meta=None,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail="A template already exists for this (platform_id, topic) scope.",
        )
    return _template_to_view(t, 0)


@app.put("/templates/{template_id}", response_model=ContentTemplateView)
async def update_template(
    template_id: str, body: UpdateTemplateRequest, _: bool = Depends(require_token),
):
    repo = _require_templates_repo()
    try:
        t = await repo.update(
            template_id=template_id,
            name=body.name,
            system_prompt=body.system_prompt,
            platform_id=body.platform_id,
            topic=body.topic,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail="Scope change collides with an existing active template.",
        )
    if t is None:
        raise HTTPException(status_code=404)
    return _template_to_view(t, await repo.count_usage(template_id=t.id))


@app.delete("/templates/{template_id}", response_model=ContentTemplateView)
async def archive_template(template_id: str, _: bool = Depends(require_token)):
    repo = _require_templates_repo()
    target = await repo.get(template_id=template_id)
    if target is None:
        raise HTTPException(status_code=404)

    # Seed protection: don't allow deleting the only active (*, *) row.
    if target.platform_id == "*" and target.topic == "*" and target.status != "archived":
        actives = await repo.list_all(platform_id="*", topic="*")
        non_archived = [t for t in actives if t.status != "archived"]
        if len(non_archived) <= 1:
            raise HTTPException(
                status_code=409,
                detail="Refusing to archive the only active (*, *) seed template. "
                       "POST a replacement first.",
            )

    archived = await repo.archive(template_id=template_id)
    return _template_to_view(archived, await repo.count_usage(template_id=archived.id))


@app.post("/templates/synthesize", response_model=ContentTemplateView, status_code=201)
async def synth_endpoint(
    body: SynthesizeRequest, _: bool = Depends(require_token),
):
    repo = _require_templates_repo()
    existing = await repo.resolve(platform_id=body.platform_id, topic=body.topic)
    if existing is not None and existing.platform_id == body.platform_id and existing.topic == body.topic:
        raise HTTPException(
            status_code=409,
            detail="An active template already exists at this scope. "
                   "DELETE it first to regenerate.",
        )

    # Pick a sample capture for synthesis input.
    sample_extracted = await _load_sample_extracted(
        sample_capture_id=body.sample_capture_id,
        platform_id=body.platform_id,
        topic=body.topic,
    )
    if sample_extracted is None:
        raise HTTPException(
            status_code=400,
            detail="No sample capture available for synthesis. Either provide "
                   "sample_capture_id or wait until at least one capture lands "
                   "in this (platform, topic) scope.",
        )

    tmpl = await synthesize_template(
        platform_id=body.platform_id,
        topic=body.topic,
        sample_extracted=sample_extracted,
        templates_repo=repo,
    )
    return _template_to_view(tmpl, 0)


async def _load_sample_extracted(
    *,
    sample_capture_id: str | None,
    platform_id: str,
    topic: str,
):
    """Load the most recent capture's extracted_snapshot for the given scope,
    or the specific row if sample_capture_id is provided. Returns an Extracted
    record or None."""
    from src.pipeline.extracted import Extracted, MediaKind
    if app_state.pool is None:
        return None
    async with app_state.pool.acquire() as conn:
        if sample_capture_id is not None:
            row = await conn.fetchrow(
                "SELECT extracted_snapshot FROM captures WHERE id = $1",
                sample_capture_id,
            )
        else:
            row = await conn.fetchrow(
                """
                SELECT extracted_snapshot FROM captures
                WHERE platform = $1 AND classifier_topic = $2
                  AND extracted_snapshot IS NOT NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                platform_id, topic,
            )
    if row is None or row["extracted_snapshot"] is None:
        return None
    import json
    snap = row["extracted_snapshot"]
    if isinstance(snap, str):
        snap = json.loads(snap)
    from datetime import datetime
    published_at = snap.get("published_at")
    return Extracted(
        title=snap.get("title"),
        body_md=snap.get("body_md", ""),
        author=snap.get("author"),
        published_at=datetime.fromisoformat(published_at) if published_at else None,
        media_kind=MediaKind(snap.get("media_kind", "video")),
        extra=snap.get("extra") or {},
    )
```

- [ ] **Step 8.5: Run test (expected: pass)**

```
cd ingest && python -m pytest tests/test_template_api.py -v
```

Expected: all 14 tests PASS.

- [ ] **Step 8.6: Commit**

```bash
git add ingest/src/api.py ingest/src/models.py ingest/tests/test_template_api.py
git commit -m "feat(ingest): /templates CRUD + /templates/resolve + /templates/synthesize"
```

---

## Task 9: /captures/{id}/rerender endpoint

**Files:**
- Modify: `ingest/src/api.py`
- Modify: `ingest/tests/test_template_api.py` — add rerender tests

- [ ] **Step 9.1: Write the failing test**

Append to `ingest/tests/test_template_api.py`:

```python
@pytest.mark.asyncio
async def test_rerender_runs_current_template_against_snapshot(client, monkeypatch):
    c, repo = client
    repo.resolve = AsyncMock(return_value=_tmpl(id="t_current"))

    # Mock a capture row with an extracted snapshot.
    captures_repo = AsyncMock()
    captures_row = MagicMock()
    captures_row.id = "cap1"
    captures_row.url = "https://example.com"
    captures_row.doc_id = "doc1"
    captures_row.platform = "youtube"
    captures_row.classifier_topic = "Tutorials"
    captures_row.extracted_snapshot = {
        "title": "T", "body_md": "B", "author": None,
        "media_kind": "video", "extra": {}, "published_at": None,
    }
    captures_repo.get_by_id = AsyncMock(return_value=captures_row)
    captures_repo.save_template_run = AsyncMock()
    monkeypatch.setattr(app_state, "captures_repo_factory",
                        lambda conn: captures_repo, raising=False)

    # Mock the render call.
    from src.pipeline.templated_render import TemplatedOutput
    fake_render = AsyncMock(return_value=TemplatedOutput(
        title="New Title", lede="The answer.",
        summary_md="- a", body_md="## Body\nContent.",
    ))
    monkeypatch.setattr("src.api.templated_render", fake_render, raising=False)

    # Mock the mcp client for block replacement.
    mcp = AsyncMock()
    monkeypatch.setattr(app_state, "mcp", mcp, raising=False)

    r = c.post("/captures/cap1/rerender", headers=HEADERS)

    assert r.status_code == 200
    fake_render.assert_awaited_once()
    captures_repo.save_template_run.assert_awaited_once()


def test_rerender_404_when_capture_missing(client, monkeypatch):
    c, repo = client
    captures_repo = AsyncMock()
    captures_repo.get_by_id = AsyncMock(return_value=None)
    monkeypatch.setattr(app_state, "captures_repo_factory",
                        lambda conn: captures_repo, raising=False)

    r = c.post("/captures/missing/rerender", headers=HEADERS)
    assert r.status_code == 404


def test_rerender_400_when_no_snapshot_and_no_reextract_flag(client, monkeypatch):
    c, repo = client
    captures_repo = AsyncMock()
    row = MagicMock()
    row.extracted_snapshot = None
    row.url = "https://example.com"
    captures_repo.get_by_id = AsyncMock(return_value=row)
    monkeypatch.setattr(app_state, "captures_repo_factory",
                        lambda conn: captures_repo, raising=False)

    r = c.post("/captures/cap1/rerender", headers=HEADERS)
    assert r.status_code == 400
    assert "reextract" in r.text.lower()
```

- [ ] **Step 9.2: Run test (expected: fail — endpoint missing)**

```
cd ingest && python -m pytest tests/test_template_api.py::test_rerender_runs_current_template_against_snapshot -v
```

Expected: FAIL with 404.

- [ ] **Step 9.3: Implement endpoint**

Append to `ingest/src/api.py`:

```python
@app.post("/captures/{capture_id}/rerender", response_model=CaptureDetail)
async def rerender_capture(
    capture_id: str,
    reextract: bool = False,
    _: bool = Depends(require_token),
):
    """Re-run the currently-resolved template against the capture's stored
    extracted_snapshot. With ?reextract=true, fall back to re-fetching the
    URL if no snapshot exists (older pre-template captures).
    """
    repo_t = _require_templates_repo()
    if app_state.pool is None:
        raise HTTPException(status_code=503, detail="DB pool not initialized")

    async with app_state.pool.acquire() as conn:
        captures_repo = CaptureRepository(conn)
        row = await captures_repo.get_by_id(capture_id)
        if row is None:
            raise HTTPException(status_code=404)

        snapshot = getattr(row, "extracted_snapshot", None)
        if snapshot is None and not reextract:
            raise HTTPException(
                status_code=400,
                detail="No extracted_snapshot for this capture. Pass "
                       "?reextract=true to refetch the source URL.",
            )

        if snapshot is None:
            raise HTTPException(
                status_code=501,
                detail="reextract=true not implemented for v1 — coming in v2.",
            )

        from src.pipeline.extracted import Extracted, MediaKind
        import json
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
        extracted = Extracted(
            title=snapshot.get("title"),
            body_md=snapshot.get("body_md", ""),
            author=snapshot.get("author"),
            published_at=None,
            media_kind=MediaKind(snapshot.get("media_kind", "video")),
            extra=snapshot.get("extra") or {},
        )

        # Resolve current template for this capture's (platform, topic).
        topic = getattr(row, "classifier_topic", None) or "*"
        template = await repo_t.resolve(platform_id=row.platform, topic=topic)
        if template is None:
            template = await repo_t.resolve(platform_id="*", topic="*")
            if template is None:
                raise HTTPException(
                    status_code=503,
                    detail="No template available — seed (*, *) is missing.",
                )

        # Run the render.
        keyframes = (extracted.extra or {}).get("keyframes") or []
        rendered = await templated_render(
            template=template, extracted=extracted, keyframes=keyframes,
        )

        # Persist the new template_run.
        await captures_repo.save_template_run(
            capture_id=row.id,
            template_id=template.id,
            prompt_used=template.system_prompt,
            output_raw=rendered.body_md,
        )

        # Replace blocks in the AFFiNE doc.
        if app_state.mcp is None or row.doc_id is None:
            log.warning("rerender: MCP unavailable or doc_id missing; skipped block update")
        else:
            from src.pipeline.markdown_render import markdown_to_blocks
            blocks: list[dict] = []
            if row.url:
                blocks.append(_url_embed_block(row.url))
            if rendered.lede:
                blocks.append({"type": "callout", "text": rendered.lede})
            if rendered.summary_md:
                blocks.append({"type": "paragraph", "style": "h2", "text": "Summary"})
                blocks.extend(
                    await markdown_to_blocks(
                        rendered.summary_md, keyframes=keyframes, mcp_client=app_state.mcp,
                    )
                )
            if rendered.body_md:
                blocks.extend(
                    await markdown_to_blocks(
                        rendered.body_md, keyframes=keyframes, mcp_client=app_state.mcp,
                    )
                )
            if row.url:
                blocks.append({
                    "type": "paragraph", "style": "text",
                    "text": [{"text": "Source: "}, {"text": row.url, "italic": True, "link": row.url}],
                })

            # Naive: wipe-and-rewrite the doc body. v2 will diff.
            # For v1, simply append the new blocks after the existing ones.
            await app_state.mcp.append_blocks(row.doc_id, blocks)

        # Refetch + return CaptureDetail.
        refreshed = await captures_repo.get_by_id(capture_id)
        return CaptureDetail(
            capture_id=refreshed.id,
            url=refreshed.url,
            platform=refreshed.platform,
            status=CaptureStatus(refreshed.status),
            doc_id=refreshed.doc_id,
            web_url=refreshed.web_url,
            topic_path=refreshed.topic_path,
            created_at=refreshed.created_at,
            classifier_reasoning=refreshed.classifier_reasoning,
            retry_count=refreshed.retry_count,
        )
```

Add to the imports at the top of `api.py`:
```python
from src.pipeline.markdown_render import markdown_to_blocks
from src.pipeline.templated_render import render as templated_render
```

Move `_url_embed_block` from `orchestrator.py` into a shared location or re-import it in `api.py`. Simplest: `from src.pipeline.orchestrator import _url_embed_block`. (If orchestrator no longer exports it after Task 7 cleanup, move the helper into a new `pipeline/url_embed.py` and import from both places.)

- [ ] **Step 9.4: Run test (expected: pass)**

```
cd ingest && python -m pytest tests/test_template_api.py -v
```

Expected: all rerender tests PASS.

- [ ] **Step 9.5: Run full ingest test suite**

```
cd ingest && python -m pytest
```

Expected: all PASS. Total new tests: ~65. Removed: ~10 (test_summarizer.py). Net delta: ~+55.

- [ ] **Step 9.6: Commit**

```bash
git add ingest/src/api.py ingest/tests/test_template_api.py
git commit -m "feat(ingest): /captures/{id}/rerender — replay template against snapshot"
```

---

## Task 10: End-to-end smoke check + macro-plan addendum

**Files:**
- Modify: `docs/plans/2026-05-06-ingest-service-macro-plan.md` — add Phase 14 row

- [ ] **Step 10.1: Add Phase 14 line to the macro plan**

Open [`docs/plans/2026-05-06-ingest-service-macro-plan.md`](2026-05-06-ingest-service-macro-plan.md). Find the phase table or list (look for "Phase 13"). Add a new line:

```
| **14** | **Content templates + rich render** | per-(platform, topic) prompts via DB, LLM synthesis fallback, rich block emitter (mermaid/embed-html/callouts/keyframe refs/cross-doc refs), 7 new API endpoints, capture-level audit trail | [`2026-05-11-phase-14-content-templates.md`](2026-05-11-phase-14-content-templates.md) |
```

(Match the existing row format — the macro plan uses the same shape for prior phases.)

- [ ] **Step 10.2: Smoke check the stack locally**

```bash
cd portainer-stack  # repo root
cd ingest && pip install -e .[dev]
cd .. && docker compose up -d postgres mcp_ext affine
# wait for postgres healthy
cd ingest && DATABASE_URL=postgresql://affine@localhost/affine_ingest \
    DB_ADMIN_URL=postgresql://affine@localhost/postgres \
    python -m src.migrate
# verify seed (*, *) row landed
docker exec affine_postgres psql -U affine -d affine_ingest \
    -c "SELECT id, platform_id, topic, status FROM content_templates;"
```

Expected: one row with `platform_id='*'`, `topic='*'`, `status='auto'`.

If the stack isn't running locally, this step can be deferred to staging. The migration tests in Task 1 already cover idempotency and seed content.

- [ ] **Step 10.3: Commit and finalize**

```bash
git add docs/plans/2026-05-06-ingest-service-macro-plan.md
git commit -m "docs(plans): add Phase 14 (content templates) to macro plan"
```

- [ ] **Step 10.4: Final test sweep**

```
cd ingest && python -m pytest -v 2>&1 | tail -30
```

Expected: ~225+ tests passing (current ~165 + ~65 new − ~10 removed).

---

## Verification checklist (engineer self-check before declaring done)

- [ ] Migration 0002 applies idempotently. The seed `(*, *)` row exists with `status='auto'`, `created_by='synth'`.
- [ ] `GET /templates/resolve?platform=youtube&topic=Tutorials` returns the seed (when no specific template exists).
- [ ] `POST /templates/synthesize` for an unknown (platform, topic) returns 201 with a new template whose `system_prompt` was produced by Sonnet 4.6 and `generator_meta.synthesizer_model == 'claude-sonnet-4-6'`.
- [ ] `POST /templates` with a duplicate active scope returns 409.
- [ ] `DELETE /templates/<seed-id>` returns 409 (seed protection).
- [ ] A new capture's row has non-null `template_id`, `template_prompt_used`, `template_output_raw`, `extracted_snapshot` after processing.
- [ ] The rendered AFFiNE doc shows: URL embed → callout (when lede) → `## Summary` + bullets → body blocks (no hardcoded `## Description` or `## Keyframes` sections).
- [ ] A markdown body containing ```mermaid renders as an `affine:code` block with `language=mermaid` in AFFiNE.
- [ ] A markdown body containing `![cap](kf:1)` renders as an `affine:image` whose `sourceId` matches the second keyframe's blob.
- [ ] `POST /captures/{id}/rerender` re-runs the current template and updates the doc; the capture row's `template_prompt_used` reflects the latest template content.
- [ ] `summarizer.py` and `test_summarizer.py` are deleted from the tree.
- [ ] `pytest` passes with no skipped non-integration tests.
