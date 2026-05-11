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
    # Chain must short-circuit on first hit — no further calls.
    assert conn.fetchrow.await_count == 1


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
    # Verify the chain queried in the correct order:
    calls = conn.fetchrow.await_args_list
    assert calls[0].args[1:] == ("youtube", "Recipes")  # most-specific first
    assert calls[1].args[1:] == ("*", "Recipes")        # topic wildcard


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
    # Verify the chain queried in the correct order:
    calls = conn.fetchrow.await_args_list
    assert calls[0].args[1:] == ("instagram", "AI")  # most-specific first
    assert calls[1].args[1:] == ("*", "AI")          # topic wildcard
    assert calls[2].args[1:] == ("instagram", "*")   # platform wildcard


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
    # Verify the chain queried in the correct order:
    calls = conn.fetchrow.await_args_list
    assert calls[0].args[1:] == ("reddit", "Politics")  # most-specific first
    assert calls[1].args[1:] == ("*", "Politics")       # topic wildcard
    assert calls[2].args[1:] == ("reddit", "*")         # platform wildcard
    assert calls[3].args[1:] == ("*", "*")              # global default


@pytest.mark.asyncio
async def test_resolve_returns_none_when_nothing_matches():
    conn = AsyncMock()
    conn.fetchrow.side_effect = [None, None, None, None]
    repo = TemplatesRepository(conn)

    tmpl = await repo.resolve(platform_id="x", topic="Memes")

    assert tmpl is None
    # Verify all 4 chain positions were tried in the correct order:
    calls = conn.fetchrow.await_args_list
    assert len(calls) == 4
    assert calls[0].args[1:] == ("x", "Memes")   # most-specific first
    assert calls[1].args[1:] == ("*", "Memes")   # topic wildcard
    assert calls[2].args[1:] == ("x", "*")       # platform wildcard
    assert calls[3].args[1:] == ("*", "*")       # global default


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
    assert "CASE WHEN status = 'auto' THEN 'edited'" in sql


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
    assert conn.fetchrow.await_count == 2


@pytest.mark.asyncio
async def test_insert_if_absent_raises_when_no_winner():
    conn = AsyncMock()
    conn.fetchrow.side_effect = [None, None]
    repo = TemplatesRepository(conn)
    with pytest.raises(RuntimeError, match="no winner found"):
        await repo.insert_if_absent(
            platform_id="youtube", topic="AI",
            name="x", system_prompt="x",
        )


@pytest.mark.asyncio
async def test_update_raises_when_no_fields_provided():
    conn = AsyncMock()
    repo = TemplatesRepository(conn)
    with pytest.raises(ValueError, match="no fields"):
        await repo.update(template_id="t_yt_tut")


@pytest.mark.asyncio
async def test_update_returns_none_when_id_not_found():
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    repo = TemplatesRepository(conn)
    assert await repo.update(template_id="nope", name="x") is None
