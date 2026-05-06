import json
from typing import Any

import httpx
import pytest

from src.mcp_client import MCPClient


def _rpc_init(req_id: Any) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}},
        },
    )


def _rpc_tool(req_id: Any, payload: Any) -> httpx.Response:
    text = json.dumps(payload) if not isinstance(payload, str) else payload
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": text}]},
        },
    )


def _make_transport(tool_payloads: list[Any]) -> tuple[httpx.MockTransport, list[dict]]:
    """Auto-handles initialize, then plays back tool results in order."""
    requests: list[dict] = []
    responses_by_method = list(tool_payloads)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        requests.append(body)
        if body["method"] == "initialize":
            return _rpc_init(body["id"])
        return _rpc_tool(body["id"], responses_by_method.pop(0))

    return httpx.MockTransport(handler), requests


@pytest.mark.asyncio
async def test_list_folder_tree_sends_correct_call():
    transport, reqs = _make_transport([{"totalNodes": 5, "tree": [{"id": "f1", "type": "folder", "name": "Sources"}]}])
    async with MCPClient("http://mcp_ext:3100", "t", _transport=transport) as c:
        result = await c.list_folder_tree()
    assert result["totalNodes"] == 5
    assert reqs[1]["params"]["name"] == "list_folder_tree"
    assert reqs[1]["params"]["arguments"] == {}


@pytest.mark.asyncio
async def test_find_doc_by_title_passes_optional_flags():
    transport, reqs = _make_transport([{"matches": []}])
    async with MCPClient("http://mcp_ext:3100", "t", _transport=transport) as c:
        await c.find_doc_by_title("Recipe", fuzzy=True, include_trash=True)
    args = reqs[1]["params"]["arguments"]
    assert args == {"title": "Recipe", "fuzzy": True, "includeTrash": True}


@pytest.mark.asyncio
async def test_find_doc_by_title_omits_falsy_flags():
    transport, reqs = _make_transport([{"matches": []}])
    async with MCPClient("http://mcp_ext:3100", "t", _transport=transport) as c:
        await c.find_doc_by_title("Recipe")
    args = reqs[1]["params"]["arguments"]
    assert args == {"title": "Recipe"}


@pytest.mark.asyncio
async def test_create_folder_with_parent():
    transport, reqs = _make_transport([{"folderId": "f2", "name": "Instagram", "parentFolderId": "f1", "index": "a0", "ok": True}])
    async with MCPClient("http://mcp_ext:3100", "t", _transport=transport) as c:
        r = await c.create_folder("Instagram", parent_folder_id="f1")
    assert r["folderId"] == "f2"
    args = reqs[1]["params"]["arguments"]
    assert args == {"name": "Instagram", "parentFolderId": "f1"}


@pytest.mark.asyncio
async def test_create_folder_no_parent_omits_field():
    transport, reqs = _make_transport([{"folderId": "f1", "name": "Sources", "parentFolderId": None, "index": "a0", "ok": True}])
    async with MCPClient("http://mcp_ext:3100", "t", _transport=transport) as c:
        await c.create_folder("Sources")
    args = reqs[1]["params"]["arguments"]
    assert args == {"name": "Sources"}


@pytest.mark.asyncio
async def test_create_doc_with_initial_blocks():
    transport, reqs = _make_transport([{"docId": "d1"}])
    async with MCPClient("http://mcp_ext:3100", "t", _transport=transport) as c:
        await c.create_doc("Hello", initial_blocks=[{"type": "paragraph", "text": "hi"}])
    args = reqs[1]["params"]["arguments"]
    assert args["title"] == "Hello"
    assert args["initialBlocks"] == [{"type": "paragraph", "text": "hi"}]


@pytest.mark.asyncio
async def test_append_blocks_with_after_heading():
    transport, reqs = _make_transport([{"appended": 1}])
    async with MCPClient("http://mcp_ext:3100", "t", _transport=transport) as c:
        await c.append_blocks("d1", [{"type": "paragraph", "text": "x"}], after_heading="AI summary")
    args = reqs[1]["params"]["arguments"]
    assert args == {
        "docId": "d1",
        "blocks": [{"type": "paragraph", "text": "x"}],
        "afterHeading": "AI summary",
    }


@pytest.mark.asyncio
async def test_move_document_to_folder():
    transport, reqs = _make_transport([{"ok": True}])
    async with MCPClient("http://mcp_ext:3100", "t", _transport=transport) as c:
        await c.move_document("d1", folder_id="f3")
    assert reqs[1]["params"]["arguments"] == {"docId": "d1", "folderId": "f3"}


@pytest.mark.asyncio
async def test_move_document_unfile_omits_folder_id():
    transport, reqs = _make_transport([{"ok": True}])
    async with MCPClient("http://mcp_ext:3100", "t", _transport=transport) as c:
        await c.move_document("d1")
    assert reqs[1]["params"]["arguments"] == {"docId": "d1"}


@pytest.mark.asyncio
async def test_delete_doc():
    transport, reqs = _make_transport([{"ok": True}])
    async with MCPClient("http://mcp_ext:3100", "t", _transport=transport) as c:
        await c.delete_doc("d1")
    assert reqs[1]["params"] == {"name": "delete_doc", "arguments": {"docId": "d1"}}
