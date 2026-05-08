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
        # Reject empty/whitespace tokens at construction. Otherwise the
        # Authorization header serializes to `Bearer ` (trailing space, no
        # credential) and httpx raises `LocalProtocolError: Illegal header
        # value b'Bearer '` on every outbound request — opaque from the
        # caller's perspective.
        token = (token or "").strip()
        if not token:
            raise ValueError(
                "MCPClient requires a non-empty bearer token "
                "(check AFFINE_ACCESS_TOKEN env var)."
            )
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

    # ── Typed tool methods ────────────────────────────────────────────

    async def list_folder_tree(self) -> dict:
        return await self.call_tool("list_folder_tree", {})

    async def find_doc_by_title(
        self,
        title: str,
        *,
        fuzzy: bool = False,
        include_trash: bool = False,
    ) -> dict:
        args: dict[str, Any] = {"title": title}
        if fuzzy:
            args["fuzzy"] = True
        if include_trash:
            args["includeTrash"] = True
        return await self.call_tool("find_doc_by_title", args)

    async def create_folder(self, name: str, *, parent_folder_id: str | None = None) -> dict:
        args: dict[str, Any] = {"name": name}
        if parent_folder_id is not None:
            args["parentFolderId"] = parent_folder_id
        return await self.call_tool("create_folder", args)

    async def create_doc(self, title: str, *, initial_blocks: list[dict] | None = None) -> dict:
        args: dict[str, Any] = {"title": title}
        if initial_blocks:
            args["initialBlocks"] = initial_blocks
        return await self.call_tool("create_doc", args)

    async def append_blocks(
        self,
        doc_id: str,
        blocks: list[dict],
        *,
        after_heading: str | None = None,
        after_block_id: str | None = None,
    ) -> dict:
        args: dict[str, Any] = {"docId": doc_id, "blocks": blocks}
        if after_heading is not None:
            args["afterHeading"] = after_heading
        if after_block_id is not None:
            args["afterBlockId"] = after_block_id
        return await self.call_tool("append_blocks", args)

    async def move_document(self, doc_id: str, *, folder_id: str | None = None) -> dict:
        args: dict[str, Any] = {"docId": doc_id}
        if folder_id is not None:
            args["folderId"] = folder_id
        return await self.call_tool("move_document", args)

    async def delete_doc(self, doc_id: str) -> dict:
        return await self.call_tool("delete_doc", {"docId": doc_id})

    async def set_doc_title(self, doc_id: str, title: str) -> dict:
        return await self.call_tool("set_doc_title", {"docId": doc_id, "title": title})

    async def list_doc_blocks(self, doc_id: str) -> dict:
        return await self.call_tool("list_doc_blocks", {"docId": doc_id})

    async def delete_block(self, doc_id: str, block_id: str) -> dict:
        return await self.call_tool("delete_block", {"docId": doc_id, "blockId": block_id})
