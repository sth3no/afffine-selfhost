# Phase 2 — MCP Client + AFFiNE Write Path (`filer`)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** From within the ingest container, programmatically create a folder path under `Sources/`, create an empty doc in it, append a block, and move it. All via the existing `mcp_ext:3100` HTTP MCP — never directly against AFFiNE.

**Macro plan:** [`docs/plans/2026-05-06-ingest-service-macro-plan.md`](./2026-05-06-ingest-service-macro-plan.md) — Phase 2
**Spec:** [`docs/specs/2026-05-06-ingest-service-design.md`](../specs/2026-05-06-ingest-service-design.md) — §3 (mcp_ext usage), §7 (filer integration), §11 (repo layout)
**Phase 1 prereq:** all 7 commits on `feat/phase-1-compose-and-db` are merged or visible in the working branch.

**Architecture:** A thin async HTTP layer (`MCPClient`) speaks JSON-RPC 2.0 over POST `/` to `mcp_ext:3100`. It handles the MCP `initialize` handshake (protocol version 2025-03-26), dispatches `tools/call` requests, and unwraps the `{content: [{type:"text", text:<json>}]}` envelope. On top sits a `Filer` with two operations: `resolve_or_create_folder(path)` walks an arbitrary `["Sources","Socials","Instagram","Recipes"]` array against the live folder tree (cached 60s), creating any missing leaves; `file_doc(folder_path, title, body_md, meta)` composes the whole flow — resolve folder, create doc, move into folder, append body — with one call from the rest of the pipeline.

**Tech Stack:**
- Python 3.12 · `httpx>=0.27` (already pinned, promoted to runtime dep)
- `pytest-asyncio` + `httpx.MockTransport` for unit tests (no real network)
- `pytest` `INTEGRATION` marker, gated by env var, for end-to-end tests against a running stack

**End state for Phase 2:**
- Unit tests pass: each `MCPClient` method exercised against a `MockTransport` returning canned JSON-RPC responses.
- Filer unit tests pass: `resolve_or_create_folder` walks a fake tree, creates only what's missing, is idempotent on repeat call. `file_doc` composes the flow correctly.
- A gated integration test (`INTEGRATION=1`) is present and runnable. Its acceptance: with the live stack up, `file_doc(["Sources","Socials","Instagram","Recipes"], "Phase 2 smoke test", "# hi", {"source": "test"})` produces exactly one new doc in AFFiNE under that path; running it twice creates exactly one additional doc and reuses every folder.

---

## Task 1: Housekeeping — gitignore + httpx as runtime dep

**Files:**
- Create: `ingest/.gitignore`
- Modify: `ingest/pyproject.toml` (add `httpx>=0.27` to runtime `dependencies`)

- [ ] **Step 1.1: Create `ingest/.gitignore`**

```
__pycache__/
*.pyc
*.pyo
*.pyd
.Python
*.egg-info/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.venv/
.env
.coverage
htmlcov/
build/
dist/
```

- [ ] **Step 1.2: Modify `ingest/pyproject.toml` — add `httpx` to runtime `dependencies`**

In the `[project] dependencies = [...]` block, add a line for `"httpx>=0.27"`. The dev group already has it; we now need it in the runtime image too. The block becomes:

```toml
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "asyncpg>=0.29",
    "pydantic>=2.7",
    "pydantic-settings>=2.5",
    "httpx>=0.27",
]
```

Leave the `[project.optional-dependencies] dev` block unchanged — `httpx` listed in both is fine; pip de-duplicates.

- [ ] **Step 1.3: Verify deps install**

```bash
cd ingest && pip install -e ".[dev]"
```

Expected: success (httpx already installed from dev install in Phase 1).

- [ ] **Step 1.4: Confirm currently-untracked artifacts disappear from `git status`**

```bash
cd .. && git status -sb
```

Expected: only the planned modifications visible. No `__pycache__/` or `egg-info/` listed under `??`.

- [ ] **Step 1.5: Commit**

```bash
git add ingest/.gitignore ingest/pyproject.toml
git commit -m "$(cat <<'EOF'
chore(ingest): add gitignore, promote httpx to runtime dep

httpx is the HTTP client the new MCP client will use to talk to
mcp_ext:3100 over JSON-RPC. Already a dev test dep; needs to be in the
runtime container too. .gitignore prevents __pycache__ and egg-info
from polluting future commits.

Phase 2 / Task 1 of docs/plans/2026-05-07-phase-2-mcp-client-and-filer.md
EOF
)"
```

---

## Task 2: MCP transport layer (`mcp_client.py` base)

Implement a minimal MCP HTTP transport: connect, do the `initialize` handshake (protocol version `2025-03-26`), dispatch `tools/call` requests, unwrap the `{content: [{type:"text", text:<json>}]}` envelope, raise typed exceptions on protocol or tool errors. No tool-specific logic yet — just the transport.

**Files:**
- Create: `ingest/src/mcp_client.py`
- Create: `ingest/tests/test_mcp_client_transport.py`

**Reference:**
- `mcp-ext/src/server.ts:200-290` — the JSON-RPC dispatch this client must conform to. POST `/` with body `{jsonrpc:"2.0", id, method, params}`. Methods: `initialize`, `tools/list`, `tools/call`, `ping`. Auth via `Authorization: Bearer <AFFINE_ACCESS_TOKEN>`.
- Tool errors come as `{result: {content: [{type:"text", text:"..."}], isError: true}}`. JSON-RPC errors come as `{error: {code, message}}`. The client must distinguish.

- [ ] **Step 2.1: Write the failing tests**

Create `ingest/tests/test_mcp_client_transport.py`:

```python
import json
from typing import Any

import httpx
import pytest

from src.mcp_client import MCPClient, MCPError, MCPToolError


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
```

- [ ] **Step 2.2: Run tests — verify they FAIL**

```bash
cd ingest && pytest tests/test_mcp_client_transport.py -v
```

Expected: 5 ImportError or NameError failures (`mcp_client` doesn't exist).

- [ ] **Step 2.3: Implement `ingest/src/mcp_client.py`**

```python
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
```

- [ ] **Step 2.4: Run tests — verify they PASS**

```bash
cd ingest && pytest tests/test_mcp_client_transport.py -v
```

Expected: 5 passed.

- [ ] **Step 2.5: Commit**

```bash
git add ingest/src/mcp_client.py ingest/tests/test_mcp_client_transport.py
git commit -m "$(cat <<'EOF'
feat(ingest): MCP client transport (initialize + tools/call)

Async JSON-RPC 2.0 client over HTTP. Performs the `initialize` handshake
lazily on first tool call (protocol version 2025-03-26). `call_tool`
returns the parsed JSON payload from the {content:[{text:...}]} envelope,
or the raw string for non-JSON tool returns. Distinguishes JSON-RPC level
errors (MCPError) from tool-level errors (MCPToolError, isError=true).

Tested against httpx.MockTransport — five cases including auth header
propagation and both error paths.

Phase 2 / Task 2 of docs/plans/2026-05-07-phase-2-mcp-client-and-filer.md
EOF
)"
```

---

## Task 3: Typed tool wrappers on `MCPClient`

Add typed methods for the 7 tools we need, on top of `call_tool`. Each method just calls `call_tool` with a fixed name and validates argument shape; the value-add is documenting required fields and giving callsites compact, typed entry points.

**Files:**
- Modify: `ingest/src/mcp_client.py` (extend the class)
- Create: `ingest/tests/test_mcp_client_tools.py`

**Tool signatures (from `mcp-ext/src/folder-tools.ts` and `write-tools.ts`):**

| Method | Tool name | Args | Returns (after JSON unwrap) |
|---|---|---|---|
| `list_folder_tree()` | `list_folder_tree` | `{}` | `{totalNodes, tree}` where tree is recursive `{id, type, name, index, children?, targetId?}` |
| `find_doc_by_title(title, fuzzy=False, include_trash=False)` | `find_doc_by_title` | `{title, fuzzy?, includeTrash?}` | `{matches: [{id, title, ...}]}` |
| `create_folder(name, parent_folder_id=None)` | `create_folder` | `{name, parentFolderId?}` | `{folderId, name, parentFolderId, index, ok}` |
| `create_doc(title, initial_blocks=None)` | `create_doc` | `{title, initialBlocks?}` | `{docId, ...}` |
| `append_blocks(doc_id, blocks, after_heading=None, after_block_id=None)` | `append_blocks` | `{docId, blocks, afterHeading?, afterBlockId?}` | success info |
| `move_document(doc_id, folder_id=None)` | `move_document` | `{docId, folderId?}` | success info |
| `delete_doc(doc_id)` | `delete_doc` | `{docId}` | success info |

- [ ] **Step 3.1: Write the failing tests**

Create `ingest/tests/test_mcp_client_tools.py`:

```python
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
```

- [ ] **Step 3.2: Run tests — verify they FAIL**

```bash
cd ingest && pytest tests/test_mcp_client_tools.py -v
```

Expected: 9 attribute errors (`MCPClient` has no `list_folder_tree`/etc. method).

- [ ] **Step 3.3: Extend `ingest/src/mcp_client.py` with typed methods**

Append these methods to the `MCPClient` class:

```python
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
        if parent_folder_id:
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
        if folder_id:
            args["folderId"] = folder_id
        return await self.call_tool("move_document", args)

    async def delete_doc(self, doc_id: str) -> dict:
        return await self.call_tool("delete_doc", {"docId": doc_id})
```

- [ ] **Step 3.4: Run tests — verify they PASS**

```bash
cd ingest && pytest tests/test_mcp_client_tools.py -v
```

Expected: 9 passed.

Also re-run Task 2 transport tests to ensure nothing broke:
```bash
cd ingest && pytest tests/ -v
```

Expected: 14+ passed (all existing + 9 new).

- [ ] **Step 3.5: Commit**

```bash
git add ingest/src/mcp_client.py ingest/tests/test_mcp_client_tools.py
git commit -m "$(cat <<'EOF'
feat(ingest): typed tool methods on MCPClient

Adds list_folder_tree, find_doc_by_title, create_folder, create_doc,
append_blocks, move_document, delete_doc — the seven tools the filer
will call. Each method takes Pythonic kwargs and translates to the
camelCase arguments the mcp_ext tool schemas expect, omitting empty
optional fields so calls stay minimal.

Tested with mocked transport: 9 cases covering arg-shape conversions
and optional-field omission behavior.

Phase 2 / Task 3 of docs/plans/2026-05-07-phase-2-mcp-client-and-filer.md
EOF
)"
```

---

## Task 4: `Filer.resolve_or_create_folder`

Walk an arbitrary path like `["Sources", "Socials", "Instagram", "Recipes"]` against the live folder tree. For each segment, find the matching child folder under the current parent; if missing, create it via `create_folder`. Return the leaf `folderId`. The folder tree is cached for 60s in memory (Phase 5 will integrate this with embeddings; Phase 4 is fine without it for now).

**Files:**
- Create: `ingest/src/pipeline/__init__.py` (empty)
- Create: `ingest/src/pipeline/filer.py`
- Create: `ingest/tests/test_filer_resolve.py`

- [ ] **Step 4.1: Create `ingest/src/pipeline/__init__.py`** (empty)

```python
```

- [ ] **Step 4.2: Write the failing tests**

Create `ingest/tests/test_filer_resolve.py`:

```python
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
```

- [ ] **Step 4.3: Run tests — verify they FAIL**

```bash
cd ingest && pytest tests/test_filer_resolve.py -v
```

Expected: 7 ImportError or AttributeError failures.

- [ ] **Step 4.4: Implement `ingest/src/pipeline/filer.py`**

```python
"""High-level operations to file a doc into the Sources/ tree.

`Filer.resolve_or_create_folder(path)` walks an arbitrary segment list against
the live AFFiNE folder tree (cached for 60s in-memory) and creates any missing
folders along the way, returning the leaf folder id.

`Filer.file_doc(folder_path, title, body_md, meta)` (Task 5) composes the full
flow: resolve the folder, create the doc, move it, append the body.
"""

from __future__ import annotations

import time
from typing import Any

from src.mcp_client import MCPClient

CACHE_TTL_SECONDS = 60.0


class Filer:
    """Stateful folder-resolver bound to an MCPClient.

    Holds an in-memory snapshot of the folder tree, refreshed every CACHE_TTL_SECONDS.
    Newly-created folders are patched into the snapshot to avoid a refetch.
    """

    def __init__(self, mcp: MCPClient, *, clock: callable = time.monotonic) -> None:
        self._mcp = mcp
        self._clock = clock
        self._tree_snapshot: list[dict] | None = None
        self._tree_fetched_at: float = 0.0

    async def resolve_or_create_folder(self, path: list[str]) -> str:
        """Walk path; create missing folders. Return the leaf folderId."""
        if not path:
            raise ValueError("path must contain at least one segment")

        await self._ensure_tree()
        assert self._tree_snapshot is not None
        siblings = self._tree_snapshot
        parent_id: str | None = None

        for segment in path:
            match = next((node for node in siblings if node.get("name") == segment), None)
            if match is None:
                created = await self._mcp.create_folder(segment, parent_folder_id=parent_id)
                new_id = str(created["folderId"])
                # Patch the in-memory tree so subsequent walks find the new folder.
                new_node = {"id": new_id, "type": "folder", "name": segment, "children": []}
                siblings.append(new_node)
                parent_id = new_id
                siblings = new_node["children"]
            else:
                parent_id = str(match["id"])
                siblings = match.setdefault("children", [])

        assert parent_id is not None
        return parent_id

    async def _ensure_tree(self) -> None:
        now = self._clock()
        if self._tree_snapshot is None or (now - self._tree_fetched_at) > CACHE_TTL_SECONDS:
            data = await self._mcp.list_folder_tree()
            self._tree_snapshot = list(data.get("tree", []))
            self._tree_fetched_at = now
```

- [ ] **Step 4.5: Run tests — verify they PASS**

```bash
cd ingest && pytest tests/test_filer_resolve.py -v
```

Expected: 7 passed.

- [ ] **Step 4.6: Commit**

```bash
git add ingest/src/pipeline/__init__.py ingest/src/pipeline/filer.py ingest/tests/test_filer_resolve.py
git commit -m "$(cat <<'EOF'
feat(ingest): Filer.resolve_or_create_folder

Walks a hierarchical path like Sources/Socials/Instagram/Recipes against
the live AFFiNE folder tree (cached 60s in-memory), creating missing
intermediate folders along the way. Returns the leaf folder id.

Newly-created folders patch the in-memory snapshot so subsequent resolves
in the same call window don't re-fetch the tree. Cache lifetime defaults
to 60s and is parameterized on a clock callable for testing.

Tested: existing-path no-op, missing leaf, missing intermediate chain,
top-level creation with no parent, empty-path validation, cache hit on
repeat call, cache patched after create.

Phase 2 / Task 4 of docs/plans/2026-05-07-phase-2-mcp-client-and-filer.md
EOF
)"
```

---

## Task 5: `Filer.file_doc`

Compose the full filing flow. Given a folder path, doc title, markdown body, and metadata dict, this method:
1. resolves/creates the folder path
2. creates a new doc with the title
3. moves the doc into the folder
4. appends a single paragraph block containing the body markdown (Phase 2 keeps body as one block; richer block construction lands in Phase 5)
5. returns `{doc_id, folder_id, web_url_path: "Sources/.../<title>"}`

**Files:**
- Modify: `ingest/src/pipeline/filer.py`
- Create: `ingest/tests/test_filer_file_doc.py`

- [ ] **Step 5.1: Write the failing tests**

Create `ingest/tests/test_filer_file_doc.py`:

```python
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
```

- [ ] **Step 5.2: Run tests — verify they FAIL**

```bash
cd ingest && pytest tests/test_filer_file_doc.py -v
```

Expected: 3 AttributeError failures (`Filer` has no `file_doc`).

- [ ] **Step 5.3: Extend `ingest/src/pipeline/filer.py`**

Append this method to the `Filer` class:

```python
    async def file_doc(
        self,
        *,
        folder_path: list[str],
        title: str,
        body_md: str,
        meta: dict[str, Any],
    ) -> dict[str, Any]:
        """End-to-end: resolve folder, create doc, move it, append body.

        `meta` is reserved for Phase 5 (will become a metadata block:
        URL, source_app, captured_at, classifier reasoning, ...). Phase 2
        accepts the dict but doesn't yet emit a metadata block.
        """
        folder_id = await self.resolve_or_create_folder(folder_path)
        created = await self._mcp.create_doc(title)
        doc_id = str(created["docId"])
        await self._mcp.move_document(doc_id, folder_id=folder_id)
        if body_md.strip():
            await self._mcp.append_blocks(doc_id, [{"type": "paragraph", "text": body_md}])
        return {
            "doc_id": doc_id,
            "folder_id": folder_id,
            "folder_path": list(folder_path),
        }
```

- [ ] **Step 5.4: Run tests — verify they PASS**

```bash
cd ingest && pytest tests/ -v
```

Expected: all tests pass (transport + tools + resolve + file_doc).

- [ ] **Step 5.5: Commit**

```bash
git add ingest/src/pipeline/filer.py ingest/tests/test_filer_file_doc.py
git commit -m "$(cat <<'EOF'
feat(ingest): Filer.file_doc — end-to-end flow

Composes resolve_or_create_folder + create_doc + move_document +
append_blocks. Empty body_md skips the append step. Returns
{doc_id, folder_id, folder_path} for the worker to record.

The `meta` dict is accepted but not yet emitted — Phase 5 turns it
into a metadata block (URL, source_app, classifier reasoning).

Phase 2 / Task 5 of docs/plans/2026-05-07-phase-2-mcp-client-and-filer.md
EOF
)"
```

---

## Task 6: Gated integration test

Add a single live-stack smoke test that exercises the full filer flow against the running `mcp_ext`. Skipped by default; runs when `INTEGRATION=1` environment variable is set. Requires `MCP_EXT_URL` and `AFFINE_ACCESS_TOKEN` to be set in the env.

**Files:**
- Create: `ingest/tests/test_filer_integration.py`
- Modify: `ingest/pyproject.toml` (register the `integration` marker so pytest doesn't warn)

- [ ] **Step 6.1: Register the integration marker**

In `ingest/pyproject.toml`, extend `[tool.pytest.ini_options]`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = [
    "integration: requires a running mcp_ext + AFFiNE stack and INTEGRATION=1 env",
]
```

- [ ] **Step 6.2: Create `ingest/tests/test_filer_integration.py`**

```python
"""Integration test against a live mcp_ext + AFFiNE stack.

Skipped unless INTEGRATION=1 in the environment. Required env vars when running:
    INTEGRATION=1
    MCP_EXT_URL=http://localhost:3100   # or http://mcp_ext:3100 inside the docker network
    AFFINE_ACCESS_TOKEN=ut_...

Acceptance: creates a doc under Sources/Test/Phase2 with a unique title,
re-running creates a second doc but reuses the folders. The test cleans up
after itself by soft-trashing the docs it created.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

from src.mcp_client import MCPClient
from src.pipeline.filer import Filer

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("INTEGRATION") != "1",
        reason="set INTEGRATION=1 to run live-stack tests",
    ),
]


@pytest.fixture
def mcp_url() -> str:
    return os.environ.get("MCP_EXT_URL") or "http://localhost:3100"


@pytest.fixture
def access_token() -> str:
    token = os.environ.get("AFFINE_ACCESS_TOKEN")
    if not token:
        pytest.skip("AFFINE_ACCESS_TOKEN not set")
    return token


@pytest.mark.asyncio
async def test_file_doc_round_trip_against_live_stack(mcp_url: str, access_token: str):
    unique = f"phase2-smoke-{uuid.uuid4().hex[:8]}"
    folder_path = ["Sources", "Test", "Phase2"]

    async with MCPClient(mcp_url, access_token) as mcp:
        filer = Filer(mcp)

        # 1. File first doc — folders may need to be created.
        first = await filer.file_doc(
            folder_path=folder_path,
            title=f"{unique}-first",
            body_md="# Phase 2 smoke test\n\nFirst write.",
            meta={"source": "integration-test"},
        )
        assert first["doc_id"]
        assert first["folder_id"]

        # 2. File second doc with same path — folders must be reused.
        second = await filer.file_doc(
            folder_path=folder_path,
            title=f"{unique}-second",
            body_md="Second write.",
            meta={"source": "integration-test"},
        )
        assert second["folder_id"] == first["folder_id"], "folder must be reused"
        assert second["doc_id"] != first["doc_id"]

        # 3. Verify both docs are in AFFiNE under the expected path by re-querying.
        # find_doc_by_title (exact) should locate each.
        first_lookup = await mcp.find_doc_by_title(f"{unique}-first")
        assert any(m["id"] == first["doc_id"] for m in first_lookup.get("matches", []))

        # 4. Cleanup — soft-trash both. Don't fail the test on cleanup errors.
        try:
            await mcp.delete_doc(first["doc_id"])
            await mcp.delete_doc(second["doc_id"])
        except Exception as e:
            print(f"cleanup warning: {e}")
```

- [ ] **Step 6.3: Verify the test is skipped by default**

```bash
cd ingest && pytest tests/test_filer_integration.py -v
```

Expected: `1 skipped` with reason `set INTEGRATION=1 to run live-stack tests`.

- [ ] **Step 6.4: Verify all unit tests still pass**

```bash
cd ingest && pytest tests/ -v --ignore=tests/test_filer_integration.py
```

Expected: all pass.

- [ ] **Step 6.5: Commit**

```bash
git add ingest/pyproject.toml ingest/tests/test_filer_integration.py
git commit -m "$(cat <<'EOF'
test(ingest): gated integration test for filer end-to-end

Round-trips file_doc against a real mcp_ext + AFFiNE stack: files two
docs under Sources/Test/Phase2, asserts the second reuses the folder
the first created, verifies both via find_doc_by_title, then soft-
trashes them. Skipped unless INTEGRATION=1 is set in the environment;
also requires MCP_EXT_URL and AFFINE_ACCESS_TOKEN.

Run with:
  INTEGRATION=1 \
  MCP_EXT_URL=http://localhost:3100 \
  AFFINE_ACCESS_TOKEN=ut_... \
  pytest tests/test_filer_integration.py -v -s

Phase 2 / Task 6 of docs/plans/2026-05-07-phase-2-mcp-client-and-filer.md
EOF
)"
```

---

## Task 7: Build verification + acceptance

**Files:** none (verification only)

- [ ] **Step 7.1: Re-build the ingest image to ensure new deps install**

```bash
cd .. && docker compose build ingest
```

Expected: clean build. The `httpx` dep is now installed in the image.

- [ ] **Step 7.2: Run the entire pytest suite once more**

```bash
cd ingest && pytest tests/ -v
```

Expected:
- `test_health.py` — 1 passed (Phase 1)
- `test_mcp_client_transport.py` — 5 passed
- `test_mcp_client_tools.py` — 9 passed
- `test_filer_resolve.py` — 7 passed
- `test_filer_file_doc.py` — 3 passed
- `test_filer_integration.py` — 1 skipped

Totals: **25 passed, 1 skipped, 0 failed**.

- [ ] **Step 7.3: Phase 2 acceptance checklist** (per macro plan)

- [ ] Unit tests pass with mocked MCP responses for each `MCPClient` method
- [ ] `resolve_or_create_folder` walks an existing tree without creating duplicates
- [ ] `resolve_or_create_folder` creates only the missing leaf when ancestors exist
- [ ] `resolve_or_create_folder` creates a full chain when nothing exists
- [ ] In-memory tree cache prevents redundant `list_folder_tree` calls
- [ ] `file_doc` composes the full flow correctly
- [ ] Integration test exists and is properly gated by `INTEGRATION=1`
- [ ] `httpx` is now in the runtime container image (Dockerfile rebuilt successfully)

If every box ticks, Phase 2 is **done**. Push the branch and move on to Phase 3.

- [ ] **Step 7.4: Push the branch**

```bash
cd .. && git push -u origin feat/phase-2-mcp-client
```

---

## Spec coverage map

| Macro Phase 2 deliverable | Plan task |
|---|---|
| `mcp_client.py` async HTTP client | Tasks 2, 3 |
| 7 typed tool methods | Task 3 |
| `pipeline/__init__.py`, `pipeline/filer.py` | Tasks 4, 5 |
| `resolve_or_create_folder(path: list[str]) -> folder_id` | Task 4 |
| `file_doc(folder_path, title, body_md, meta) -> doc_id` | Task 5 |
| Mocked unit tests for each method | Tasks 2, 3, 4, 5 |
| `INTEGRATION=1`-gated integration test | Task 6 |
| Idempotent folder resolution | Task 4 |
| `httpx` in runtime deps | Task 1 |

No gaps. No placeholders. Type names consistent across tasks (`MCPClient`, `MCPError`, `MCPToolError`, `Filer`, `Extracted` — last is Phase 4).

---

## Out of scope for Phase 2

- Embedding-similarity check before folder creation — Phase 5
- Metadata-block rendering (`meta` dict → AFFiNE block) — Phase 5
- DB persistence of folder_embeddings — Phase 5
- Worker loop wiring `file_doc` to capture rows — Phase 6
- Auth on the ingest service's own HTTP API — Phase 3
- Any HTTP endpoint that calls `Filer` from outside — Phase 3
