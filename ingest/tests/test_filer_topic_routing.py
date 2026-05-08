from unittest.mock import AsyncMock, MagicMock

import pytest

from src.db import FolderEmbeddingRow
from src.pipeline.classification import ClassificationResult
from src.pipeline.filer import Filer


def _make_filer():
    mcp = AsyncMock()
    mcp.list_folder_tree.return_value = {"totalNodes": 0, "tree": [
        {"id": "f-sources", "type": "folder", "name": "Sources", "children": [
            {"id": "f-socials", "type": "folder", "name": "Socials", "children": [
                {"id": "f-ig", "type": "folder", "name": "Instagram", "children": [
                    {"id": "f-recipes", "type": "folder", "name": "Recipes", "children": []},
                ]},
            ]},
        ]},
    ]}
    embeddings_repo = AsyncMock()
    aliases_repo = AsyncMock()
    embed_fn = AsyncMock(return_value=[0.1] * 1536)
    return Filer(mcp, embeddings_repo=embeddings_repo, aliases_repo=aliases_repo, embed_fn=embed_fn), mcp, embeddings_repo, aliases_repo, embed_fn


@pytest.mark.asyncio
async def test_move_to_topic_uses_existing_alias_first():
    filer, mcp, embeddings_repo, aliases_repo, embed_fn = _make_filer()
    aliases_repo.lookup.return_value = "Recipes"  # existing alias
    embeddings_repo.list_for_parent.return_value = [
        FolderEmbeddingRow(folder_id="f-recipes", folder_name="Recipes",
                           parent_path="Sources/Socials/Instagram",
                           embedding=[0.1] * 1536),
    ]

    result = ClassificationResult(topic="Cooking", confidence=0.9, reasoning="x")
    folder_id = await filer.move_to_topic_folder(
        platform_path=["Sources", "Socials", "Instagram"],
        result=result,
    )

    assert folder_id == "f-recipes"
    aliases_repo.lookup.assert_awaited_once()
    # Alias hit short-circuits embedding work.
    embed_fn.assert_not_called()
    # No new folder created.
    mcp.create_folder.assert_not_called()


@pytest.mark.asyncio
async def test_move_to_topic_explicit_alias_of_skips_embedding():
    filer, mcp, embeddings_repo, aliases_repo, embed_fn = _make_filer()
    aliases_repo.lookup.return_value = None
    embeddings_repo.list_for_parent.return_value = [
        FolderEmbeddingRow(folder_id="f-recipes", folder_name="Recipes",
                           parent_path="Sources/Socials/Instagram",
                           embedding=[0.1] * 1536),
    ]

    result = ClassificationResult(topic="Cooking", confidence=0.9, reasoning="x", alias_of="Recipes")
    folder_id = await filer.move_to_topic_folder(
        platform_path=["Sources", "Socials", "Instagram"],
        result=result,
    )

    assert folder_id == "f-recipes"
    embed_fn.assert_not_called()
    aliases_repo.record.assert_awaited_once()
    args = aliases_repo.record.call_args.kwargs
    assert args["alias"] == "Cooking"
    assert args["canonical"] == "Recipes"


@pytest.mark.asyncio
async def test_move_to_topic_high_similarity_collapses():
    """Proposed 'Cooking' with embedding cosine > 0.85 vs existing 'Recipes' → collapse."""
    filer, mcp, embeddings_repo, aliases_repo, embed_fn = _make_filer()
    aliases_repo.lookup.return_value = None
    embeddings_repo.list_for_parent.return_value = [
        FolderEmbeddingRow(folder_id="f-recipes", folder_name="Recipes",
                           parent_path="Sources/Socials/Instagram",
                           embedding=[1.0, 0.0, 0.0] + [0.0] * 1533),
    ]
    embed_fn.return_value = [0.99, 0.01, 0.0] + [0.0] * 1533  # cosine ≈ 0.9999

    result = ClassificationResult(topic="Cooking", confidence=0.9, reasoning="x")
    folder_id = await filer.move_to_topic_folder(
        platform_path=["Sources", "Socials", "Instagram"],
        result=result,
    )

    assert folder_id == "f-recipes"
    aliases_repo.record.assert_awaited_once()
    mcp.create_folder.assert_not_called()


@pytest.mark.asyncio
async def test_move_to_topic_low_similarity_creates_new():
    filer, mcp, embeddings_repo, aliases_repo, embed_fn = _make_filer()
    aliases_repo.lookup.return_value = None
    embeddings_repo.list_for_parent.return_value = [
        FolderEmbeddingRow(folder_id="f-recipes", folder_name="Recipes",
                           parent_path="Sources/Socials/Instagram",
                           embedding=[1.0, 0.0] + [0.0] * 1534),
    ]
    embed_fn.return_value = [0.0, 1.0] + [0.0] * 1534  # orthogonal: cosine = 0

    mcp.create_folder.return_value = {"folderId": "f-workouts-new", "ok": True}
    result = ClassificationResult(topic="Workouts", confidence=0.9, reasoning="gym")
    folder_id = await filer.move_to_topic_folder(
        platform_path=["Sources", "Socials", "Instagram"],
        result=result,
    )

    assert folder_id == "f-workouts-new"
    mcp.create_folder.assert_awaited_once_with("Workouts", parent_folder_id="f-ig")
    embeddings_repo.upsert.assert_awaited_once()


@pytest.mark.asyncio
async def test_move_to_topic_returns_none_when_topic_is_none():
    """Low-confidence classification → topic=None → leave doc at platform root."""
    filer, _, _, _, _ = _make_filer()
    result = ClassificationResult(topic=None, confidence=0.3, reasoning="ambiguous")
    folder_id = await filer.move_to_topic_folder(
        platform_path=["Sources", "Socials", "Instagram"],
        result=result,
    )
    assert folder_id is None


# ── Pool-wiring path (production) ──────────────────────────────────


@pytest.mark.asyncio
async def test_move_to_topic_raises_clearly_when_neither_pool_nor_repos():
    """Regression: api.py wired Filer(mcp) without pool or repos and every
    confident-classified capture failed at the filing step. The error
    message must guide the operator to the right fix."""
    mcp = AsyncMock()
    mcp.list_folder_tree.return_value = {"totalNodes": 0, "tree": []}
    embed_fn = AsyncMock(return_value=[0.1] * 1536)
    filer = Filer(mcp, embed_fn=embed_fn)  # no pool, no repos

    with pytest.raises(RuntimeError, match="requires either pool or embeddings_repo"):
        await filer.move_to_topic_folder(
            platform_path=["Sources", "Socials", "Instagram"],
            result=ClassificationResult(topic="Recipes", confidence=0.9, reasoning="x"),
        )


@pytest.mark.asyncio
async def test_move_to_topic_acquires_repos_from_pool():
    """Production path: Filer(pool=...) acquires a connection per call and
    builds repos against it."""
    mcp = AsyncMock()
    mcp.list_folder_tree.return_value = {"totalNodes": 0, "tree": [
        {"id": "f-sources", "type": "folder", "name": "Sources", "children": [
            {"id": "f-socials", "type": "folder", "name": "Socials", "children": [
                {"id": "f-ig", "type": "folder", "name": "Instagram", "children": []},
            ]},
        ]},
    ]}
    mcp.create_folder.return_value = {"folderId": "f-new", "ok": True}

    # Mock the asyncpg pool.acquire() context manager → connection.
    fake_conn = MagicMock()
    fake_conn.fetchval = AsyncMock(return_value=None)  # alias miss
    fake_conn.fetch = AsyncMock(return_value=[])        # no existing embeddings
    fake_conn.execute = AsyncMock()                     # upsert + record

    fake_pool = MagicMock()
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=fake_conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)
    fake_pool.acquire = MagicMock(return_value=acquire_ctx)

    embed_fn = AsyncMock(return_value=[0.1] * 1536)
    filer = Filer(mcp, pool=fake_pool, embed_fn=embed_fn)

    result = ClassificationResult(topic="Music", confidence=0.9, reasoning="x")
    folder_id = await filer.move_to_topic_folder(
        platform_path=["Sources", "Socials", "Instagram"],
        result=result,
    )

    assert folder_id == "f-new"
    fake_pool.acquire.assert_called_once()  # exactly one acquire per call
    mcp.create_folder.assert_awaited_once_with("Music", parent_folder_id="f-ig")
