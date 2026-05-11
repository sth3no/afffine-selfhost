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
