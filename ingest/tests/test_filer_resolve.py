from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.pipeline.filer import Filer


def _tree(*folders: dict[str, Any]) -> dict:
    """Build a folder-tree response. Each folder dict: {id, name, children?}."""
    def to_node(f: dict) -> dict:
        return {
            "id": f["id"],
            "type": "folder",
            "name": f["name"],
            "index": f.get("index", "a0"),
            "children": [to_node(c) for c in f.get("children", [])],
        }
    return {"totalNodes": 0, "tree": [to_node(f) for f in folders]}


@pytest.mark.asyncio
async def test_resolve_or_create_folder_existing_path_no_creates():
    mcp = AsyncMock()
    mcp.list_folder_tree.return_value = _tree(
        {"id": "f-sources", "name": "Sources", "children": [
            {"id": "f-socials", "name": "Socials", "children": [
                {"id": "f-ig", "name": "Instagram", "children": [
                    {"id": "f-recipes", "name": "Recipes"},
                ]},
            ]},
        ]}
    )
    filer = Filer(mcp)

    folder_id = await filer.resolve_or_create_folder(["Sources", "Socials", "Instagram", "Recipes"])

    assert folder_id == "f-recipes"
    mcp.create_folder.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_or_create_folder_missing_leaf_creates_only_leaf():
    mcp = AsyncMock()
    mcp.list_folder_tree.return_value = _tree(
        {"id": "f-sources", "name": "Sources", "children": [
            {"id": "f-socials", "name": "Socials", "children": [
                {"id": "f-ig", "name": "Instagram"},
            ]},
        ]}
    )
    mcp.create_folder.return_value = {"folderId": "f-recipes-new", "ok": True}
    filer = Filer(mcp)

    folder_id = await filer.resolve_or_create_folder(["Sources", "Socials", "Instagram", "Recipes"])

    assert folder_id == "f-recipes-new"
    mcp.create_folder.assert_called_once_with("Recipes", parent_folder_id="f-ig")


@pytest.mark.asyncio
async def test_resolve_or_create_folder_missing_intermediate_creates_each():
    mcp = AsyncMock()
    mcp.list_folder_tree.return_value = _tree(
        {"id": "f-sources", "name": "Sources"},
    )

    created = iter([
        {"folderId": "f-socials-new", "ok": True},
        {"folderId": "f-ig-new", "ok": True},
        {"folderId": "f-recipes-new", "ok": True},
    ])
    mcp.create_folder.side_effect = lambda name, **_: next(created)

    filer = Filer(mcp)
    folder_id = await filer.resolve_or_create_folder(["Sources", "Socials", "Instagram", "Recipes"])

    assert folder_id == "f-recipes-new"
    assert mcp.create_folder.call_count == 3
    calls = [c.args[0] for c in mcp.create_folder.call_args_list]
    assert calls == ["Socials", "Instagram", "Recipes"]
    parents = [c.kwargs["parent_folder_id"] for c in mcp.create_folder.call_args_list]
    assert parents == ["f-sources", "f-socials-new", "f-ig-new"]


@pytest.mark.asyncio
async def test_resolve_or_create_folder_root_creates_top_level_no_parent():
    mcp = AsyncMock()
    mcp.list_folder_tree.return_value = _tree()  # empty tree
    mcp.create_folder.return_value = {"folderId": "f-sources-new", "ok": True}
    filer = Filer(mcp)

    folder_id = await filer.resolve_or_create_folder(["Sources"])

    assert folder_id == "f-sources-new"
    mcp.create_folder.assert_called_once_with("Sources", parent_folder_id=None)


@pytest.mark.asyncio
async def test_resolve_or_create_folder_empty_path_raises():
    mcp = AsyncMock()
    filer = Filer(mcp)
    with pytest.raises(ValueError):
        await filer.resolve_or_create_folder([])


@pytest.mark.asyncio
async def test_resolve_or_create_folder_caches_tree_for_subsequent_calls():
    """Within a 60s window, list_folder_tree shouldn't be called twice."""
    mcp = AsyncMock()
    mcp.list_folder_tree.return_value = _tree(
        {"id": "f-sources", "name": "Sources", "children": [
            {"id": "f-socials", "name": "Socials"},
        ]}
    )
    filer = Filer(mcp)

    await filer.resolve_or_create_folder(["Sources", "Socials"])
    await filer.resolve_or_create_folder(["Sources", "Socials"])

    assert mcp.list_folder_tree.call_count == 1


@pytest.mark.asyncio
async def test_resolve_or_create_folder_invalidates_cache_after_creation():
    """After creating a new folder, the in-memory tree must be patched so that
    a follow-up resolve in the same call window finds it without another fetch."""
    mcp = AsyncMock()
    mcp.list_folder_tree.return_value = _tree(
        {"id": "f-sources", "name": "Sources"},
    )
    mcp.create_folder.return_value = {"folderId": "f-news", "ok": True}
    filer = Filer(mcp)

    first = await filer.resolve_or_create_folder(["Sources", "News"])
    second = await filer.resolve_or_create_folder(["Sources", "News"])

    assert first == "f-news"
    assert second == "f-news"
    # Tree fetched once total; the create_folder call patched the in-memory tree.
    assert mcp.list_folder_tree.call_count == 1
    assert mcp.create_folder.call_count == 1
