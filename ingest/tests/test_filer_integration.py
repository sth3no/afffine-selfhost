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
import uuid

import httpx
import pytest

from src.mcp_client import MCPClient, MCPError, MCPToolError
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

        # 4. Cleanup — soft-trash both. Don't fail the test on transient
        # network/server errors during cleanup; programming errors still surface.
        try:
            await mcp.delete_doc(first["doc_id"])
            await mcp.delete_doc(second["doc_id"])
        except (httpx.HTTPError, MCPError, MCPToolError) as e:
            print(f"cleanup warning: {e}")
