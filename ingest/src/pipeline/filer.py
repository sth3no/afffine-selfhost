"""High-level operations to file a doc into the Sources/ tree.

`Filer.resolve_or_create_folder(path)` walks an arbitrary segment list against
the live AFFiNE folder tree (cached for 60s in-memory) and creates any missing
folders along the way, returning the leaf folder id.

`Filer.file_doc(folder_path, title, body_md, meta)` (Task 5) composes the full
flow: resolve the folder, create the doc, move it, append the body.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from src.mcp_client import MCPClient

CACHE_TTL_SECONDS = 60.0


class Filer:
    """Stateful folder-resolver bound to an MCPClient.

    Holds an in-memory snapshot of the folder tree, refreshed every CACHE_TTL_SECONDS.
    Newly-created folders are patched into the snapshot to avoid a refetch.
    """

    def __init__(self, mcp: MCPClient, *, clock: Callable[[], float] = time.monotonic) -> None:
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

    async def _ensure_tree(self) -> None:
        now = self._clock()
        if self._tree_snapshot is None or (now - self._tree_fetched_at) > CACHE_TTL_SECONDS:
            data = await self._mcp.list_folder_tree()
            self._tree_snapshot = list(data.get("tree", []))
            self._tree_fetched_at = now
