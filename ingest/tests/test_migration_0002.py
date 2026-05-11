"""Integration test for migration 0002 (content_templates + seed).

Skipped unless DB_ADMIN_URL is set (CI provides it; local dev opts in
via .env). Runs both 0001 and 0002 on a throwaway database, then asserts
the schema and seed row.
"""

import os
import uuid

import asyncpg
import pytest

from src.migrate import apply_migrations

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
    try:
        await apply_migrations(target)
        yield target
    finally:
        conn = await asyncpg.connect(admin)
        try:
            await conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
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
            "WHERE table_name='captures' AND table_schema='public'"
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
