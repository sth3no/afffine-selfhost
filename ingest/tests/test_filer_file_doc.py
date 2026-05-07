from unittest.mock import AsyncMock

import pytest

from src.pipeline.filer import Filer


@pytest.mark.asyncio
async def test_file_doc_happy_path_calls_in_order():
    mcp = AsyncMock()
    mcp.list_folder_tree.return_value = {"totalNodes": 1, "tree": [
        {"id": "f-sources", "type": "folder", "name": "Sources", "children": [
            {"id": "f-articles", "type": "folder", "name": "Articles", "children": []},
        ]},
    ]}
    mcp.create_doc.return_value = {"docId": "doc-123"}
    mcp.move_document.return_value = {"ok": True}
    mcp.append_blocks.return_value = {"appended": 1}

    filer = Filer(mcp)

    result = await filer.file_doc(
        folder_path=["Sources", "Articles"],
        title="Hello world",
        body_md="# Hello\n\nThis is a body.",
        meta={"source_app": "Safari", "url": "https://example.com"},
    )

    assert result["doc_id"] == "doc-123"
    assert result["folder_id"] == "f-articles"
    assert result["folder_path"] == ["Sources", "Articles"]

    mcp.create_doc.assert_called_once_with("Hello world")
    mcp.move_document.assert_called_once_with("doc-123", folder_id="f-articles")
    # append_blocks called with a single block carrying the body markdown
    args, kwargs = mcp.append_blocks.call_args
    assert args[0] == "doc-123"
    blocks = args[1]
    assert isinstance(blocks, list) and len(blocks) == 1
    assert "Hello" in blocks[0]["text"] or "body" in blocks[0]["text"]


@pytest.mark.asyncio
async def test_file_doc_creates_missing_folder_chain():
    mcp = AsyncMock()
    mcp.list_folder_tree.return_value = {"totalNodes": 0, "tree": []}
    mcp.create_folder.side_effect = [
        {"folderId": "f-sources"}, {"folderId": "f-socials"}, {"folderId": "f-ig"}, {"folderId": "f-recipes"},
    ]
    mcp.create_doc.return_value = {"docId": "doc-1"}
    mcp.append_blocks.return_value = {"appended": 1}

    filer = Filer(mcp)
    result = await filer.file_doc(
        folder_path=["Sources", "Socials", "Instagram", "Recipes"],
        title="Honey-glazed salmon",
        body_md="Recipe body",
        meta={},
    )

    assert mcp.create_folder.call_count == 4
    assert result["doc_id"] == "doc-1"
    assert result["folder_id"] == "f-recipes"


@pytest.mark.asyncio
async def test_file_doc_skips_append_for_empty_body():
    mcp = AsyncMock()
    mcp.list_folder_tree.return_value = {"totalNodes": 1, "tree": [
        {"id": "f-sources", "type": "folder", "name": "Sources", "children": []},
    ]}
    mcp.create_doc.return_value = {"docId": "doc-1"}

    filer = Filer(mcp)
    await filer.file_doc(
        folder_path=["Sources"],
        title="Stub",
        body_md="",
        meta={},
    )

    mcp.append_blocks.assert_not_called()
