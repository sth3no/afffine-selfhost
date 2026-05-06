"""Async MCP client for talking to mcp_ext over JSON-RPC 2.0 over HTTP.

Speaks the streamable-HTTP transport (single POST / per request), which is
what mcp_ext exposes. The MCP `initialize` handshake is performed lazily on
the first tool call.

Tool results from mcp_ext are wrapped as {content: [{type:"text", text:str}]}.
The text payload is itself JSON-encoded for structured tools (every tool we
care about is structured) — this client tries `json.loads` first and falls
back to the raw string if that fails. Tool errors arrive with `isError: true`
and become MCPToolError. JSON-RPC errors become MCPError.
"""

from __future__ import annotations

import json
from itertools import count
from typing import Any

import httpx

PROTOCOL_VERSION = "2025-03-26"
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


class MCPError(Exception):
    """JSON-RPC level error from the MCP server."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"MCP error {code}: {message}")
        self.code = code
        self.message = message


class MCPToolError(Exception):
    """Tool returned isError=true. The text content is the message."""

    def __init__(self, tool_name: str, message: str) -> None:
        super().__init__(f"{tool_name}: {message}")
        self.tool_name = tool_name
        self.message = message


class MCPClient:
    """Async client. Use as `async with MCPClient(...) as c: await c.call_tool(...)`."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        *,
        _transport: httpx.AsyncBaseTransport | httpx.MockTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._transport = _transport
        self._client: httpx.AsyncClient | None = None
        self._initialized = False
        self._counter = count(1)

    async def __aenter__(self) -> "MCPClient":
        kwargs: dict[str, Any] = {
            "base_url": self._base_url,
            "timeout": self._timeout,
            "headers": {
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        self._client = httpx.AsyncClient(**kwargs)
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def initialize(self) -> dict:
        """Perform the MCP initialize handshake. Idempotent."""
        if self._initialized:
            return {}
        result = await self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "affine-ingest", "version": "0.1.0"},
            },
        )
        self._initialized = True
        return result

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call an MCP tool. Returns the parsed JSON payload, or the raw text
        if the tool returned a non-JSON string."""
        await self.initialize()
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments})

        content = result.get("content", [])
        is_error = bool(result.get("isError"))
        text = ""
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                break

        if is_error:
            raise MCPToolError(name, text)

        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text

    async def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        if self._client is None:
            raise RuntimeError("MCPClient must be used inside `async with`")
        req_id = next(self._counter)
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        response = await self._client.post("/", json=payload)
        response.raise_for_status()
        body = response.json()
        if "error" in body:
            err = body["error"]
            raise MCPError(int(err.get("code", -1)), str(err.get("message", "")))
        return body.get("result", {})

