import json
from typing import Any

import httpx
import pytest

from src.mcp_client import MCPClient, MCPError, MCPToolError


@pytest.mark.parametrize("bad_token", ["", " ", "  \t\n"])
def test_mcp_client_rejects_empty_token(bad_token: str):
    """Empty / whitespace-only tokens must fail at construction.

    Regression: production sent `Authorization: Bearer ` (trailing space,
    no credential) when the token env var was unset, which httpx then
    rejected with an opaque `LocalProtocolError: Illegal header value
    b'Bearer '` on every outbound /capture call.
    """
    with pytest.raises(ValueError, match="non-empty bearer token"):
        MCPClient("http://mcp_internal:3000/mcp", bad_token)


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
    """Records POST bodies and returns canned responses in order.

    Notifications (no `id`) are answered with 202 and recorded separately,
    so the canned responses line up with real requests only."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.requests: list[dict] = []
        self.notifications: list[dict] = []
        self.session_headers: list[str | None] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if "id" not in body:
            self.notifications.append(body)
            return httpx.Response(202)
        self.requests.append(body)
        self.session_headers.append(request.headers.get("mcp-session-id"))
        if not self._responses:
            raise AssertionError(f"Unexpected request: {body}")
        return self._responses.pop(0)


def _sse_response(req_id: Any, result: Any, session: str | None = None) -> httpx.Response:
    headers = {"Content-Type": "text/event-stream"}
    if session:
        headers["mcp-session-id"] = session
    data = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result})
    return httpx.Response(200, content=f"event: message\ndata: {data}\n\n".encode(), headers=headers)


@pytest.mark.asyncio
async def test_sse_responses_and_session_id_are_handled():
    """DAWNCR0W answers with text/event-stream and hands out an mcp-session-id
    on initialize that every later request must echo back."""
    transport = _FakeTransport(
        [
            _sse_response(1, {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}, session="s-1"),
            _sse_response(2, {"content": [{"type": "text", "text": json.dumps({"ok": True})}]}),
        ]
    )
    async with MCPClient("http://mcp_internal:3000/mcp", "t", _transport=httpx.MockTransport(transport)) as client:
        result = await client.call_tool("current_user", {})

    assert result == {"ok": True}
    assert transport.session_headers == [None, "s-1"]
    assert [n["method"] for n in transport.notifications] == ["notifications/initialized"]


@pytest.mark.asyncio
async def test_expired_session_reinitializes_and_retries_once():
    """After the server's idle timeout the session is gone (404). The client
    must start a new session and replay the call instead of failing."""
    transport = _FakeTransport(
        [
            _sse_response(1, {"protocolVersion": "2025-03-26"}, session="old"),
            httpx.Response(404, json={"jsonrpc": "2.0", "error": {"code": -32001, "message": "Session not found"}, "id": None}),
            _sse_response(3, {"protocolVersion": "2025-03-26"}, session="new"),
            _sse_response(4, {"content": [{"type": "text", "text": "\"done\""}]}),
        ]
    )
    async with MCPClient("http://mcp_internal:3000/mcp", "t", _transport=httpx.MockTransport(transport)) as client:
        await client.initialize()
        result = await client.call_tool("current_user", {})

    assert result == "done"
    assert [r["method"] for r in transport.requests] == ["initialize", "tools/call", "initialize", "tools/call"]
    assert transport.session_headers == [None, "old", None, "new"]


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
        base_url="http://mcp_internal:3000/mcp",
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
        base_url="http://mcp_internal:3000/mcp",
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
        base_url="http://mcp_internal:3000/mcp",
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
        if "id" not in body:  # notifications/initialized
            return httpx.Response(202)
        return _rpc_response(body["id"], {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}})

    async with MCPClient(
        base_url="http://mcp_internal:3000/mcp",
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
        base_url="http://mcp_internal:3000/mcp",
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
        base_url="http://mcp_internal:3000/mcp",
        token="t",
        _transport=httpx.MockTransport(transport),
    ) as client:
        result = await client.call_tool("ping", {})

    # Non-JSON text returned as-is in a string.
    assert result == "ok"


@pytest.mark.asyncio
async def test_concurrent_calls_share_one_reinitialize_after_expiry():
    """Two worker loops hitting an expired session at once must start ONE new
    session, and neither may send a request without a session id."""
    import asyncio

    state = {"session": "old", "inits": 0}
    sessionless_calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        sid = request.headers.get("mcp-session-id")
        if body.get("method") == "initialize":
            state["inits"] += 1
            await asyncio.sleep(0.01)
            state["session"] = f"s{state['inits']}"
            return _sse_response(body["id"], {}, session=state["session"])
        if "id" not in body:
            await asyncio.sleep(0.01)
            return httpx.Response(202)
        if sid is None:
            sessionless_calls.append(body)
            return httpx.Response(400, json={"error": "No valid session ID provided"})
        if sid != state["session"]:
            return httpx.Response(404, json={"jsonrpc": "2.0", "error": {"code": -32001, "message": "Session not found"}, "id": None})
        return _sse_response(body["id"], {"content": [{"type": "text", "text": "\"ok\""}]})

    async with MCPClient("http://mcp_internal:3000/mcp", "t", _transport=httpx.MockTransport(handler)) as client:
        await client.initialize()
        state["session"] = "server-restarted"  # every known session is now invalid
        results = await asyncio.gather(*(client.call_tool("current_user", {}) for _ in range(3)))

    assert results == ["ok", "ok", "ok"]
    assert sessionless_calls == []
    assert state["inits"] == 2
