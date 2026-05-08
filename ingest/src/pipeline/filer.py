"""High-level operations to file a doc into the Sources/ tree.

`Filer.resolve_or_create_folder(path)` walks an arbitrary segment list against
the live AFFiNE folder tree (cached for 60s in-memory) and creates any missing
folders along the way, returning the leaf folder id.

`Filer.file_doc(folder_path, title, body_md, meta)` (Task 5) composes the full
flow: resolve the folder, create the doc, move it, append the body.

`Filer.move_to_topic_folder(platform_path, result)` (Phase 5 Task 5) resolves
or creates the topic subfolder under the platform using embedding-similarity
dedup to prevent duplicate folders.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from src.mcp_client import MCPClient
from src.pipeline.classification import ClassificationResult
from src.pipeline.embeddings import cosine_similarity

CACHE_TTL_SECONDS = 60.0


class Filer:
    """Stateful folder-resolver bound to an MCPClient.

    Holds an in-memory snapshot of the folder tree, refreshed every CACHE_TTL_SECONDS.
    Newly-created folders are patched into the snapshot to avoid a refetch.
    """

    def __init__(
        self,
        mcp: MCPClient,
        *,
        clock: Callable[[], float] = time.monotonic,
        pool: Any = None,
        embeddings_repo: Any = None,
        aliases_repo: Any = None,
        embed_fn: Any = None,
        similarity_threshold: float = 0.85,
    ) -> None:
        """Construct a Filer.

        Production wiring: pass `pool` (asyncpg pool) + `embed_fn`. Each
        `move_to_topic_folder` call acquires a fresh connection from the
        pool and builds repos against it — avoids serializing concurrent
        captures on a single shared connection.

        Test wiring: pass `embeddings_repo` + `aliases_repo` directly to
        skip the pool dependency. `embed_fn` is still required.
        """
        self._mcp = mcp
        self._clock = clock
        self._tree_snapshot: list[dict] | None = None
        self._tree_fetched_at: float = 0.0
        self._pool = pool
        self._embeddings_repo = embeddings_repo
        self._aliases_repo = aliases_repo
        self._embed_fn = embed_fn
        self._similarity_threshold = similarity_threshold

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

    async def move_to_topic_folder(
        self,
        *,
        platform_path: list[str],
        result: ClassificationResult,
    ) -> str | None:
        """Resolve / create the topic subfolder under the given platform.

        Returns the folder_id, or None when result.topic is None
        (confidence-floor case → caller leaves doc at platform_path root).
        """
        if result.topic is None:
            return None
        if self._embed_fn is None:
            raise RuntimeError("move_to_topic_folder requires embed_fn")

        # Test wiring: repos pre-injected → use them directly.
        if self._embeddings_repo is not None and self._aliases_repo is not None:
            return await self._do_move(
                platform_path=platform_path,
                result=result,
                embeddings_repo=self._embeddings_repo,
                aliases_repo=self._aliases_repo,
            )

        # Production wiring: acquire a fresh connection per call so
        # concurrent classifications don't serialize on one shared conn.
        if self._pool is None:
            raise RuntimeError(
                "move_to_topic_folder requires either pool or "
                "embeddings_repo + aliases_repo to be provided",
            )

        from src.db import FolderEmbeddingRepository, TopicAliasRepository

        async with self._pool.acquire() as conn:
            return await self._do_move(
                platform_path=platform_path,
                result=result,
                embeddings_repo=FolderEmbeddingRepository(conn),
                aliases_repo=TopicAliasRepository(conn),
            )

    async def _do_move(
        self,
        *,
        platform_path: list[str],
        result: ClassificationResult,
        embeddings_repo: Any,
        aliases_repo: Any,
    ) -> str | None:
        """Topic-folder resolution + creation logic. Repos are passed in so
        the same logic works whether they're long-lived (tests) or per-call
        (production via pool acquisition)."""
        platform_folder_id = await self.resolve_or_create_folder(platform_path)
        parent_path = "/".join(platform_path)

        # 1. Pre-recorded alias hit?
        alias_canonical = await aliases_repo.lookup(parent_path=parent_path, alias=result.topic)
        if alias_canonical is not None:
            return await self._resolve_existing_topic_folder(platform_folder_id, alias_canonical)

        # 2. Explicit alias_of from classifier?
        if result.alias_of:
            folder_id = await self._resolve_existing_topic_folder(platform_folder_id, result.alias_of)
            await aliases_repo.record(
                parent_path=parent_path, alias=result.topic, canonical=result.alias_of,
            )
            return folder_id

        # 3. Embedding-similarity check against existing siblings.
        proposed_vec = await self._embed_fn(result.topic)
        siblings = await embeddings_repo.list_for_parent(parent_path)
        best_match: tuple[str, str, float] | None = None  # (folder_id, name, cosine)
        for sib in siblings:
            sim = cosine_similarity(proposed_vec, sib.embedding)
            if sim >= self._similarity_threshold and (best_match is None or sim > best_match[2]):
                best_match = (sib.folder_id, sib.folder_name, sim)
        if best_match is not None:
            await aliases_repo.record(
                parent_path=parent_path, alias=result.topic, canonical=best_match[1],
            )
            return best_match[0]

        # 4. Create new folder under platform + persist embedding.
        from src.db import FolderEmbeddingRow

        created = await self._mcp.create_folder(result.topic, parent_folder_id=platform_folder_id)
        new_folder_id = str(created["folderId"])
        await embeddings_repo.upsert(FolderEmbeddingRow(
            folder_id=new_folder_id,
            folder_name=result.topic,
            parent_path=parent_path,
            embedding=list(proposed_vec),
        ))
        # Patch in-memory tree so subsequent resolves find the new folder.
        self._patch_tree(platform_path, result.topic, new_folder_id)
        return new_folder_id

    async def _resolve_existing_topic_folder(self, platform_folder_id: str, name: str) -> str:
        """Find a child folder by name under platform_folder_id; raise if missing.

        Used when an alias points us at a name we expect to already exist.
        """
        await self._ensure_tree()
        target = self._find_node_by_id(platform_folder_id)
        if target is None:
            raise LookupError(f"platform folder {platform_folder_id!r} not in tree snapshot")
        for child in target.get("children", []):
            if child.get("name") == name:
                return str(child["id"])
        raise LookupError(f"alias canonical {name!r} not found under {platform_folder_id!r}")

    def _find_node_by_id(self, folder_id: str) -> dict | None:
        """DFS for a folder node in the cached tree."""
        if self._tree_snapshot is None:
            return None
        stack = list(self._tree_snapshot)
        while stack:
            node = stack.pop()
            if node.get("id") == folder_id:
                return node
            stack.extend(node.get("children", []))
        return None

    def _patch_tree(self, platform_path: list[str], topic: str, new_folder_id: str) -> None:
        """Append a new child node under the platform parent in the cached tree."""
        siblings = self._tree_snapshot or []
        for segment in platform_path:
            match = next((n for n in siblings if n.get("name") == segment), None)
            if match is None:
                return  # tree shape doesn't match; bail without crashing
            siblings = match.setdefault("children", [])
        siblings.append({"id": new_folder_id, "type": "folder", "name": topic, "children": []})
