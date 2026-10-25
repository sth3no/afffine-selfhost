import base64
import json
from typing import Any

import httpx
import pytest

from src.mcp_client import MCPClient

URL = "http://mcp_internal:3000/mcp"


def _make_transport(payloads: dict[str, list[Any]]) -> tuple[httpx.MockTransport, list[dict]]:
    """Answers initialize / notifications itself, then plays back tool results per tool name."""
    calls: list[dict] = []
    queues = {name: list(items) for name, items in payloads.items()}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})
        if "id" not in body:
            return httpx.Response(202)
        name = body["params"]["name"]
        calls.append({"name": name, "arguments": body["params"]["arguments"]})
        payload = queues[name].pop(0)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": json.dumps(payload)}]}},
        )

    return httpx.MockTransport(handler), calls


def _nodes(*nodes: dict) -> dict:
    return {"workspaceId": "ws", "nodes": list(nodes)}


@pytest.mark.asyncio
async def test_list_folder_tree_builds_nested_tree_from_organize_nodes():
    transport, calls = _make_transport({"list_organize_nodes": [_nodes(
        {"id": "l1", "parentId": "f2", "type": "doc", "data": "d1", "index": "a1"},
        {"id": "f2", "parentId": "f1", "type": "folder", "data": "Socials", "index": "a0"},
        {"id": "f1", "parentId": None, "type": "folder", "data": "Sources", "index": "a0"},
        {"id": "f3", "parentId": "f1", "type": "folder", "data": "News", "index": "Z0"},
    )]})
    async with MCPClient(URL, "t", _transport=transport) as c:
        result = await c.list_folder_tree()
    assert calls == [{"name": "list_organize_nodes", "arguments": {}}]
    assert result["totalNodes"] == 4
    assert result["tree"] == [{
        "id": "f1", "type": "folder", "name": "Sources", "children": [
            {"id": "f3", "type": "folder", "name": "News", "children": []},
            {"id": "f2", "type": "folder", "name": "Socials", "children": [
                {"id": "l1", "type": "doc", "docId": "d1", "children": []},
            ]},
        ],
    }]


@pytest.mark.asyncio
async def test_find_doc_by_title_maps_fuzzy_and_filters_trash():
    matches = {"matches": [{"id": "d1", "inTrash": False}, {"id": "d2", "inTrash": True}]}
    transport, calls = _make_transport({"find_doc_by_title": [matches, matches]})
    async with MCPClient(URL, "t", _transport=transport) as c:
        live = await c.find_doc_by_title("Recipe", fuzzy=True)
        everything = await c.find_doc_by_title("Recipe", include_trash=True)
    assert calls[0]["arguments"] == {"title": "Recipe", "caseInsensitive": True}
    assert calls[1]["arguments"] == {"title": "Recipe"}
    assert [m["id"] for m in live["matches"]] == ["d1"]
    assert [m["id"] for m in everything["matches"]] == ["d1", "d2"]


@pytest.mark.asyncio
async def test_create_folder_with_and_without_parent_returns_folder_id():
    transport, calls = _make_transport({"create_folder": [{"id": "f2", "type": "folder"}, {"id": "f1", "type": "folder"}]})
    async with MCPClient(URL, "t", _transport=transport) as c:
        child = await c.create_folder("Instagram", parent_folder_id="f1")
        root = await c.create_folder("Sources")
    assert child["folderId"] == "f2" and root["folderId"] == "f1"
    assert calls[0]["arguments"] == {"name": "Instagram", "parentId": "f1"}
    assert calls[1]["arguments"] == {"name": "Sources"}


@pytest.mark.asyncio
async def test_create_doc_appends_initial_blocks():
    transport, calls = _make_transport({
        "create_doc": [{"docId": "d1", "ok": True}],
        "append_block": [{"blockId": "b1", "ok": True}],
    })
    async with MCPClient(URL, "t", _transport=transport) as c:
        created = await c.create_doc("Hello", initial_blocks=[{"type": "paragraph", "text": "hi"}])
    assert created["docId"] == "d1"
    assert calls == [
        {"name": "create_doc", "arguments": {"title": "Hello"}},
        {"name": "append_block", "arguments": {"docId": "d1", "type": "paragraph", "text": "hi"}},
    ]


@pytest.mark.asyncio
async def test_append_blocks_without_anchor_appends_in_order():
    transport, calls = _make_transport({"append_block": [{"blockId": "b1"}, {"blockId": "b2"}]})
    async with MCPClient(URL, "t", _transport=transport) as c:
        result = await c.append_blocks("d1", [{"type": "divider"}, {"type": "paragraph", "style": "h2", "text": "S"}])
    assert result == {"appended": 2, "blockIds": ["b1", "b2"]}
    assert calls[0]["arguments"] == {"docId": "d1", "type": "divider"}
    assert calls[1]["arguments"] == {"docId": "d1", "type": "heading", "level": 2, "text": "S"}


@pytest.mark.asyncio
async def test_append_blocks_after_block_chains_placement():
    transport, calls = _make_transport({"append_block": [{"blockId": "b1"}, {"blockId": "b2"}]})
    async with MCPClient(URL, "t", _transport=transport) as c:
        await c.append_blocks("d1", [{"type": "divider"}, {"type": "divider"}], after_block_id="b0")
    assert calls[0]["arguments"]["placement"] == {"afterBlockId": "b0"}
    assert calls[1]["arguments"]["placement"] == {"afterBlockId": "b1"}


@pytest.mark.asyncio
async def test_append_blocks_after_heading_finds_heading_block():
    transport, calls = _make_transport({
        "read_doc": [{"blocks": [
            {"id": "p1", "flavour": "affine:paragraph", "type": "text", "text": "AI summary"},
            {"id": "h1", "flavour": "affine:paragraph", "type": "h3", "text": "AI summary"},
        ]}],
        "append_block": [{"blockId": "b1"}],
    })
    async with MCPClient(URL, "t", _transport=transport) as c:
        await c.append_blocks("d1", [{"type": "paragraph", "text": "x"}], after_heading="AI summary")
    assert calls[0] == {"name": "read_doc", "arguments": {"docId": "d1"}}
    assert calls[1]["arguments"]["placement"] == {"afterBlockId": "h1"}


@pytest.mark.asyncio
async def test_append_blocks_after_missing_heading_raises():
    transport, _ = _make_transport({"read_doc": [{"blocks": []}]})
    async with MCPClient(URL, "t", _transport=transport) as c:
        with pytest.raises(LookupError, match="heading"):
            await c.append_blocks("d1", [{"type": "divider"}], after_heading="Nope")


@pytest.mark.asyncio
async def test_move_document_links_target_then_removes_old_links():
    transport, calls = _make_transport({
        "list_organize_nodes": [_nodes(
            {"id": "l1", "parentId": "f1", "type": "doc", "data": "d1", "index": "a0"},
            {"id": "l9", "parentId": "f1", "type": "doc", "data": "d9", "index": "a1"},
        )],
        "add_organize_link": [{"id": "l2"}],
        "delete_organize_link": [{"success": True}],
    })
    async with MCPClient(URL, "t", _transport=transport) as c:
        await c.move_document("d1", folder_id="f3")
    assert calls[1] == {"name": "add_organize_link", "arguments": {"folderId": "f3", "type": "doc", "targetId": "d1"}}
    assert calls[2] == {"name": "delete_organize_link", "arguments": {"nodeId": "l1"}}
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_move_document_already_in_target_is_a_no_op():
    transport, calls = _make_transport({"list_organize_nodes": [_nodes(
        {"id": "l1", "parentId": "f3", "type": "doc", "data": "d1", "index": "a0"},
    )]})
    async with MCPClient(URL, "t", _transport=transport) as c:
        await c.move_document("d1", folder_id="f3")
    assert [x["name"] for x in calls] == ["list_organize_nodes"]


@pytest.mark.asyncio
async def test_move_document_without_folder_unfiles():
    transport, calls = _make_transport({
        "list_organize_nodes": [_nodes({"id": "l1", "parentId": "f1", "type": "doc", "data": "d1", "index": "a0"})],
        "delete_organize_link": [{"success": True}],
    })
    async with MCPClient(URL, "t", _transport=transport) as c:
        await c.move_document("d1")
    assert calls[1] == {"name": "delete_organize_link", "arguments": {"nodeId": "l1"}}


@pytest.mark.asyncio
async def test_simple_doc_tools_map_names_and_arguments():
    transport, calls = _make_transport({
        "delete_doc": [{"ok": True}],
        "update_doc_title": [{"ok": True}],
        "read_doc": [{"blocks": [{"id": "b1"}]}],
        "delete_block": [{"deleted": True}],
    })
    async with MCPClient(URL, "t", _transport=transport) as c:
        await c.delete_doc("d1")
        await c.set_doc_title("d1", "New")
        listing = await c.list_doc_blocks("d1")
        await c.delete_block("d1", "b1")
    assert calls == [
        {"name": "delete_doc", "arguments": {"docId": "d1", "confirmDocId": "d1"}},
        {"name": "update_doc_title", "arguments": {"docId": "d1", "title": "New"}},
        {"name": "read_doc", "arguments": {"docId": "d1"}},
        {"name": "delete_block", "arguments": {"docId": "d1", "blockId": "b1"}},
    ]
    assert listing["blocks"] == [{"id": "b1"}]


@pytest.mark.asyncio
async def test_upload_blob_sends_base64_with_workspace_and_returns_source_id():
    transport, calls = _make_transport({"upload_blob": [{"id": "cap-frame-01.jpg", "key": "cap-frame-01.jpg", "size": 3}]})
    async with MCPClient(URL, "t", workspace_id="ws1", _transport=transport) as c:
        result = await c.upload_blob(filename="cap-frame-01.jpg", content_type="image/jpeg", data=b"abc")
    assert result == {"sourceId": "cap-frame-01.jpg", "byteCount": 3, "ok": True}
    assert calls[0]["arguments"] == {
        "workspaceId": "ws1",
        "content": base64.b64encode(b"abc").decode(),
        "encoding": "base64",
        "filename": "cap-frame-01.jpg",
        "contentType": "image/jpeg",
    }


@pytest.mark.asyncio
async def test_upload_blob_without_workspace_raises():
    transport, _ = _make_transport({})
    async with MCPClient(URL, "t", _transport=transport) as c:
        with pytest.raises(ValueError, match="workspace"):
            await c.upload_blob(filename="x.jpg", content_type="image/jpeg", data=b"x")
