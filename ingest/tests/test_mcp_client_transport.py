import json
from typing import Any

import httpx
import pytest

from src.mcp_client import MCPClient, MCPError, MCPToolError


@pytest.mark.parametrize("bad_token", ["", " ", "  \t\n"])
def test_mcp_client_rejects_empty_token(bad_token: str):
    """Empty / whitespace-only tokens must fail at construction.

    Regression: production sent `Authorization: Bearer ` (trailing space,
    no credential) when AFFINE_ACCESS_TOKEN was unset, which httpx then
    rejected with an opaque `LocalProtocolError: Illegal header value
    b'Bearer '` on every outbound /capture call.
    """
    with pytest.raises(ValueError, match="non-empty bearer token"):
        MCPClient("http://mcp_ext:3100", bad_token)


def _rpc_response(req_id: Any, result: Any) -> httpx.Response:
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "id": req_id, "result": result},
        headers={"Content-Type": "application/json"},
    )


def _rpc_error_response(req_id: Any, code: int, message: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
        headers={"Content-Type": "application/json"},
    )


class _FakeTransport:
    """Records POST bodies and returns canned responses in order."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        self.requests.append(body)
        if not self._responses:
            raise AssertionError(f"Unexpected request: {body}")
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_initialize_then_call_tool_unwraps_text_content():
    transport = _FakeTransport(
        [
            _rpc_response(1, {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}),
            _rpc_response(
                2,
                {"content": [{"type": "text", "text": json.dumps({"folderId": "f1", "ok": True})}]},
            ),
        ]
    )
    async with MCPClient(
        base_url="http://mcp_ext:3100",
        token="test-token",
        _transport=httpx.MockTransport(transport),
    ) as client:
        result = await client.call_tool("create_folder", {"name": "Sources"})

    assert result == {"folderId": "f1", "ok": True}
    assert transport.requests[0]["method"] == "initialize"
    assert transport.requests[0]["params"]["protocolVersion"] == "2025-03-26"
    assert transport.requests[1]["method"] == "tools/call"
    assert transport.requests[1]["params"]["name"] == "create_folder"
    assert transport.requests[1]["params"]["arguments"] == {"name": "Sources"}


@pytest.mark.asyncio
async def test_call_tool_raises_mcp_tool_error_on_isError():
    transport = _FakeTransport(
        [
            _rpc_response(1, {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}),
            _rpc_response(
                2,
                {"content": [{"type": "text", "text": "Tool 'create_folder' failed: parent missing"}], "isError": True},
            ),
        ]
    )
    async with MCPClient(
        base_url="http://mcp_ext:3100",
        token="test-token",
        _transport=httpx.MockTransport(transport),
    ) as client:
        with pytest.raises(MCPToolError) as exc_info:
            await client.call_tool("create_folder", {"name": "Sources"})
        assert "parent missing" in str(exc_info.value)


@pytest.mark.asyncio
async def test_call_tool_raises_mcp_error_on_jsonrpc_error():
    transport = _FakeTransport(
        [
            _rpc_response(1, {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}),
            _rpc_error_response(2, -32602, "Missing tool name"),
        ]
    )
    async with MCPClient(
        base_url="http://mcp_ext:3100",
        token="test-token",
        _transport=httpx.MockTransport(transport),
    ) as client:
        with pytest.raises(MCPError) as exc_info:
            await client.call_tool("create_folder", {"name": "Sources"})
        assert "Missing tool name" in str(exc_info.value)
        assert exc_info.value.code == -32602


@pytest.mark.asyncio
async def test_authorization_header_sent():
    transport = _FakeTransport(
        [
            _rpc_response(1, {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}),
        ]
    )
    seen_headers: dict = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen_headers.update(dict(request.headers))
        body = json.loads(request.content.decode())
        return _rpc_response(body["id"], {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}})

    async with MCPClient(
        base_url="http://mcp_ext:3100",
        token="ut_secret",
        _transport=httpx.MockTransport(capture),
    ) as client:
        await client.initialize()

    assert seen_headers.get("authorization") == "Bearer ut_secret"


@pytest.mark.asyncio
async def test_http_5xx_propagates_as_httpx_error():
    """Locking in the contract: server-side 5xx surfaces as httpx.HTTPStatusError,
    NOT as MCPError or MCPToolError. Phase 4+ Filer callers will need to catch
    httpx.HTTPError if they want to handle transient server outages."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "upstream unavailable"})

    async with MCPClient(
        base_url="http://mcp_ext:3100",
        token="t",
        _transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.initialize()


@pytest.mark.asyncio
async def test_string_text_passes_through_when_not_json():
    """Some tools return plain prose strings (errors, notes), not JSON."""
    transport = _FakeTransport(
        [
            _rpc_response(1, {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}),
            _rpc_response(2, {"content": [{"type": "text", "text": "ok"}]}),
        ]
    )
    async with MCPClient(
        base_url="http://mcp_ext:3100",
        token="t",
        _transport=httpx.MockTransport(transport),
    ) as client:
        result = await client.call_tool("ping", {})

    # Non-JSON text returned as-is in a string.
    assert result == "ok"
