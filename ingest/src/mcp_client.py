"""Async MCP client for the AFFiNE MCP server (DAWNCR0W `affine-mcp-server`).

Talks to the internal bearer-mode instance (`mcp_internal`) over the
streamable-HTTP transport: one POST per JSON-RPC message to the `/mcp`
endpoint, answered as JSON or as a server-sent event. The server hands out
an `mcp-session-id` on `initialize`; every later request echoes it, and when
the session has expired (HTTP 404) the client starts a new one and replays
the call once. The handshake is performed lazily on the first tool call.

Tool results are wrapped as {content: [{type:"text", text:str}]}. The text
payload is JSON for structured tools — this client tries `json.loads` first
and falls back to the raw string. Tool errors arrive with `isError: true`
and become MCPToolError. JSON-RPC errors become MCPError.

The typed methods keep the interface the pipeline was written against
(folder tree with names, `folderId` / `docId` / `sourceId` results, block
specs from `markdown_render`) and translate it to DAWNCR0W's tools.
"""

from __future__ import annotations

import asyncio
import base64
import json
from itertools import count
from typing import Any

import httpx

from src.dawn_blocks import to_append_block_args

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


class _SessionExpired(Exception):
    """The server no longer knows the mcp-session-id the request carried
    (idle timeout, restart)."""

    def __init__(self, session_id: str) -> None:
        super().__init__(session_id)
        self.session_id = session_id


class MCPClient:
    """Async client. Use as `async with MCPClient(...) as c: await c.call_tool(...)`."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        *,
        workspace_id: str | None = None,
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
                "(check AFFINE_MCP_HTTP_TOKEN env var)."
            )
        self._url = base_url
        self._token = token
        self._timeout = timeout
        self._workspace_id = workspace_id or None
        self._transport = _transport
        self._client: httpx.AsyncClient | None = None
        self._initialized = False
        self._session_id: str | None = None
        # Serializes (re)initialization: worker loops and /capture share one
        # client, and must not each start a session or send a request while
        # the session id is being replaced.
        self._init_lock = asyncio.Lock()
        self._counter = count(1)

    async def __aenter__(self) -> "MCPClient":
        kwargs: dict[str, Any] = {
            "timeout": self._timeout,
            "headers": {
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
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
        """Perform the MCP initialize handshake. Idempotent and safe to call
        concurrently: only one handshake runs, the others wait for it."""
        if self._initialized:
            return {}
        async with self._init_lock:
            if self._initialized:
                return {}
            result = await self._rpc(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": "affine-ingest", "version": "0.2.0"},
                },
                session=None,
            )
            await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, self._session_id)
            self._initialized = True
            return result

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call an MCP tool. Returns the parsed JSON payload, or the raw text
        if the tool returned a non-JSON string."""
        await self.initialize()
        params = {"name": name, "arguments": arguments}
        try:
            result = await self._rpc("tools/call", params, session=self._session_id)
        except _SessionExpired as expired:
            # Only the first caller that sees this session die resets it;
            # the others find a newer session already in place.
            if self._session_id == expired.session_id:
                self._initialized = False
            await self.initialize()
            result = await self._rpc("tools/call", params, session=self._session_id)

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

    async def _post(self, payload: dict[str, Any], session: str | None) -> httpx.Response:
        if self._client is None:
            raise RuntimeError("MCPClient must be used inside `async with`")
        headers = {"mcp-session-id": session} if session else {}
        response = await self._client.post(self._url, json=payload, headers=headers)
        if response.status_code == 404 and session:
            raise _SessionExpired(session)
        response.raise_for_status()
        return response

    async def _rpc(self, method: str, params: dict[str, Any], *, session: str | None) -> Any:
        req_id = next(self._counter)
        response = await self._post({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}, session)
        if method == "initialize":
            self._session_id = response.headers.get("mcp-session-id")
        body = _parse_body(response, req_id)
        if "error" in body:
            err = body["error"]
            raise MCPError(int(err.get("code", -1)), str(err.get("message", "")))
        return body.get("result", {})

    # ── Typed tool methods ────────────────────────────────────────────

    async def list_folder_tree(self) -> dict:
        """Organize sidebar as a tree: folders `{id, type, name, children}`,
        doc links `{id, type: "doc", docId, children: []}`, siblings in
        sidebar order."""
        data = await self.call_tool("list_organize_nodes", {})
        nodes = data.get("nodes", [])
        built: dict[str, dict] = {}
        for node in nodes:
            if node.get("type") == "folder":
                built[node["id"]] = {"id": node["id"], "type": "folder", "name": node.get("data"), "children": []}
            else:
                built[node["id"]] = {"id": node["id"], "type": node.get("type"), "docId": node.get("data"), "children": []}
        roots: list[dict] = []
        for node in sorted(nodes, key=lambda n: n.get("index") or ""):
            parent = built.get(node.get("parentId") or "")
            (parent["children"] if parent else roots).append(built[node["id"]])
        return {"totalNodes": len(nodes), "tree": roots}

    async def find_doc_by_title(
        self,
        title: str,
        *,
        fuzzy: bool = False,
        include_trash: bool = False,
    ) -> dict:
        args: dict[str, Any] = {"title": title}
        if fuzzy:
            args["caseInsensitive"] = True
        result = await self.call_tool("find_doc_by_title", args)
        if not include_trash and isinstance(result, dict):
            result = {**result, "matches": [m for m in result.get("matches", []) if not m.get("inTrash")]}
        return result

    async def create_folder(self, name: str, *, parent_folder_id: str | None = None) -> dict:
        args: dict[str, Any] = {"name": name}
        if parent_folder_id is not None:
            args["parentId"] = parent_folder_id
        node = await self.call_tool("create_folder", args)
        return {**node, "folderId": node["id"]}

    async def create_doc(self, title: str, *, initial_blocks: list[dict] | None = None) -> dict:
        created = await self.call_tool("create_doc", {"title": title})
        if initial_blocks:
            await self.append_blocks(created["docId"], initial_blocks)
        return created

    async def append_blocks(
        self,
        doc_id: str,
        blocks: list[dict],
        *,
        after_heading: str | None = None,
        after_block_id: str | None = None,
    ) -> dict:
        """Append block specs in order, one `append_block` call each. With an
        anchor (a block id, or the text of a heading), the blocks go right
        after it, chained so they keep their order.

        All-or-nothing as far as the server lets us: every spec is converted
        before the first write, and when a write fails midway the blocks this
        call already added are deleted again before the error propagates — so
        a retried capture does not end up with a half or doubled body."""
        converted = [to_append_block_args(spec) for spec in blocks]
        anchor = after_block_id
        if after_heading is not None:
            anchor = await self._find_heading(doc_id, after_heading)
        block_ids: list[str] = []
        try:
            for block_args in converted:
                args = {"docId": doc_id, **block_args}
                if anchor:
                    args["placement"] = {"afterBlockId": anchor}
                result = await self.call_tool("append_block", args)
                block_ids.append(result.get("blockId"))
                if anchor:
                    anchor = result.get("blockId") or anchor
        except Exception:
            for block_id in reversed([b for b in block_ids if b]):
                try:
                    await self.call_tool("delete_block", {"docId": doc_id, "blockId": block_id, "deleteChildren": True})
                except Exception:  # noqa: BLE001 — best effort; the original error matters
                    pass
            raise
        return {"appended": len(block_ids), "blockIds": block_ids}

    async def _find_heading(self, doc_id: str, heading: str) -> str:
        listing = await self.call_tool("read_doc", {"docId": doc_id})
        for block in listing.get("blocks", []):
            if (
                block.get("flavour") == "affine:paragraph"
                and str(block.get("type") or "").startswith("h")
                and (block.get("text") or "").strip() == heading
            ):
                return block["id"]
        raise LookupError(f"heading {heading!r} not found in doc {doc_id}")

    async def move_document(self, doc_id: str, *, folder_id: str | None = None) -> dict:
        """File the doc into exactly one folder (or none when folder_id is
        None): link it to the target, then drop its other sidebar links."""
        data = await self.call_tool("list_organize_nodes", {})
        nodes = data.get("nodes", [])
        if folder_id is not None and not any(n.get("type") == "folder" and n.get("id") == folder_id for n in nodes):
            # A stale id (folder deleted by hand) must not unfile the doc.
            raise LookupError(f"folder {folder_id} does not exist")
        links = [n for n in nodes if n.get("type") == "doc" and n.get("data") == doc_id]
        if folder_id is not None and not any(n.get("parentId") == folder_id for n in links):
            await self.call_tool("add_organize_link", {"folderId": folder_id, "type": "doc", "targetId": doc_id})
        for link in links:
            if link.get("parentId") != folder_id:
                await self.call_tool("delete_organize_link", {"nodeId": link["id"]})
        return {"docId": doc_id, "folderId": folder_id, "ok": True}

    async def delete_doc(self, doc_id: str) -> dict:
        """Move the doc to AFFiNE's trash (recoverable), never a hard delete."""
        return await self.call_tool("trash_doc", {"docId": doc_id})

    async def set_doc_title(self, doc_id: str, title: str) -> dict:
        return await self.call_tool("update_doc_title", {"docId": doc_id, "title": title})

    async def list_doc_blocks(self, doc_id: str) -> dict:
        """`{blocks: [{id, flavour, type, text, ...}]}` via `read_doc`."""
        return await self.call_tool("read_doc", {"docId": doc_id})

    async def delete_block(self, doc_id: str, block_id: str) -> dict:
        return await self.call_tool("delete_block", {"docId": doc_id, "blockId": block_id})

    async def upload_blob(
        self,
        *,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> dict:
        """Upload binary bytes to AFFiNE workspace blob storage.

        Returns `{sourceId, byteCount, ok}`. The `sourceId` (the blob key,
        which is the filename) is what an `image` block references, so
        filenames must be unique per workspace."""
        if not self._workspace_id:
            raise ValueError("upload_blob needs the AFFiNE workspace id (AFFINE_WORKSPACE_ID)")
        result = await self.call_tool(
            "upload_blob",
            {
                "workspaceId": self._workspace_id,
                "content": base64.b64encode(data).decode("ascii"),
                "encoding": "base64",
                "filename": filename,
                "contentType": content_type,
            },
        )
        return {"sourceId": result.get("key") or result.get("id"), "byteCount": result.get("size"), "ok": True}


def _parse_body(response: httpx.Response, req_id: int) -> dict:
    """JSON-RPC message from a JSON body or from the matching SSE `data:` line."""
    if not response.headers.get("content-type", "").startswith("text/event-stream"):
        return response.json()
    last: dict = {}
    for line in response.text.splitlines():
        if line.startswith("data:"):
            message = json.loads(line[5:].strip())
            if message.get("id") == req_id:
                return message
            last = message
    return last
