from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.db import (
    FolderEmbeddingRepository,
    FolderEmbeddingRow,
    TopicAliasRepository,
)


@pytest.mark.asyncio
async def test_folder_embedding_upsert_executes_correct_sql():
    conn = AsyncMock()
    repo = FolderEmbeddingRepository(conn)
    await repo.upsert(FolderEmbeddingRow(
        folder_id="f1",
        folder_name="Recipes",
        parent_path="Sources/Socials/Instagram",
        embedding=[0.1, 0.2, 0.3],
    ))
    sql, *args = conn.execute.call_args.args
    assert "INSERT INTO folder_embeddings" in sql
    assert "ON CONFLICT" in sql
    assert args[0] == "f1"
    assert args[1] == "Recipes"
    assert args[2] == "Sources/Socials/Instagram"


@pytest.mark.asyncio
async def test_folder_embedding_list_for_parent_returns_rows():
    conn = AsyncMock()
    conn.fetch.return_value = [
        {"folder_id": "f1", "folder_name": "Recipes",
         "parent_path": "Sources/Socials/Instagram",
         "embedding": "[0.1,0.2,0.3]",
         "updated_at": datetime(2026, 5, 7, tzinfo=timezone.utc)},
    ]
    repo = FolderEmbeddingRepository(conn)
    rows = await repo.list_for_parent("Sources/Socials/Instagram")
    assert len(rows) == 1
    assert rows[0].folder_name == "Recipes"
    assert rows[0].embedding == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_topic_alias_record_executes_insert():
    conn = AsyncMock()
    repo = TopicAliasRepository(conn)
    await repo.record(parent_path="Sources/Socials/Instagram", alias="Cooking", canonical="Recipes")
    sql, *args = conn.execute.call_args.args
    assert "INSERT INTO topic_aliases" in sql
    assert "ON CONFLICT" in sql
    assert args == ["Sources/Socials/Instagram", "Cooking", "Recipes"]


@pytest.mark.asyncio
async def test_topic_alias_lookup_returns_canonical_when_present():
    conn = AsyncMock()
    conn.fetchval.return_value = "Recipes"
    repo = TopicAliasRepository(conn)
    canonical = await repo.lookup(parent_path="Sources/Socials/Instagram", alias="Cooking")
    assert canonical == "Recipes"


@pytest.mark.asyncio
async def test_topic_alias_lookup_returns_none_when_absent():
    conn = AsyncMock()
    conn.fetchval.return_value = None
    repo = TopicAliasRepository(conn)
    assert await repo.lookup(parent_path="x", alias="y") is None
