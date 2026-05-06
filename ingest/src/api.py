"""FastAPI app for the ingest service.

Phase 3 ships /health (Phase 1) and POST /capture. List/get/retry/delete
endpoints land in Phase 7. Worker loop in Phase 6.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, status
from ulid import ULID

from src.auth import require_token
from src.config import load_topics, settings
from src.db import CaptureRepository, CaptureRow, create_pool
from src.mcp_client import MCPClient
from src.models import (
    CaptureRequest,
    CaptureResponse,
    CaptureStatus,
    normalized_url,
    url_hash,
)
from src.pipeline.filer import Filer
from src.pipeline.router import PlatformRouter


# ── Application-scoped state ──────────────────────────────────────────


class AppState:
    """Mutable container set during lifespan startup."""
    pool: asyncpg.Pool | None = None
    mcp: MCPClient | None = None
    filer: Filer | None = None
    router: PlatformRouter | None = None


app_state = AppState()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup: load topics, open DB pool, prepare MCP client + filer.
    Shutdown: close everything."""
    # Topics & router (load once; hot-reload deferred to a future phase).
    topics = load_topics()
    app_state.router = PlatformRouter(topics)

    # MCP client to mcp_ext (lazy connect — no network call here).
    mcp_url = os.environ.get("MCP_EXT_URL", "http://mcp_ext:3100")
    affine_token = os.environ.get("AFFINE_ACCESS_TOKEN", "")
    app_state.mcp = await MCPClient(mcp_url, affine_token).__aenter__()
    app_state.filer = Filer(app_state.mcp)

    # asyncpg pool (skipped when DATABASE_URL points at the placeholder used
    # by `pip install` smoke tests on developer machines).
    if settings.database_url and "placeholder" not in settings.database_url:
        app_state.pool = await create_pool(settings.database_url)

    yield

    # Shutdown
    if app_state.pool is not None:
        await app_state.pool.close()
    if app_state.mcp is not None:
        await app_state.mcp.__aexit__(None, None, None)


app = FastAPI(title="affine-ingest", version=settings.version, lifespan=lifespan)


# ── DI providers (overrideable in tests) ──────────────────────────────


async def get_pool() -> asyncpg.Pool:
    if app_state.pool is None:
        raise HTTPException(status_code=503, detail="Database pool not initialized")
    return app_state.pool


async def get_capture_repo(pool: asyncpg.Pool = Depends(get_pool)) -> CaptureRepository:
    # Acquire a connection per request. Pool returns it on context exit.
    async with pool.acquire() as conn:
        yield CaptureRepository(conn)


def get_filer() -> Filer:
    if app_state.filer is None:
        raise HTTPException(status_code=503, detail="MCP filer not initialized")
    return app_state.filer


def get_platform_router() -> PlatformRouter:
    if app_state.router is None:
        raise HTTPException(status_code=503, detail="Platform router not initialized")
    return app_state.router


# ── Routes ────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "queue_depth": 0,
        "worker_alive": False,
        "version": settings.version,
    }


@app.post("/capture", response_model=CaptureResponse, status_code=status.HTTP_202_ACCEPTED)
async def capture(
    body: CaptureRequest,
    repo: CaptureRepository = Depends(get_capture_repo),
    filer: Filer = Depends(get_filer),
    router: PlatformRouter = Depends(get_platform_router),
    _: str = require_token,
) -> CaptureResponse:
    if not body.url and not body.shared_text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one of url or shared_text must be present.",
        )

    # 1. Idempotency: hash the (normalized) URL and look up existing.
    if body.url:
        hash_value = url_hash(body.url)
        existing = await repo.get_by_url_hash(hash_value)
        if existing is not None:
            return _row_to_response(existing, router)
    else:
        # No URL — text-only capture; idempotency by hash of text.
        hash_value = url_hash(body.shared_text or "")  # not URL-shaped but the function still hashes
        existing = await repo.get_by_url_hash(hash_value)
        if existing is not None:
            return _row_to_response(existing, router)

    # 2. Detect platform from URL (text-only capture defaults to article).
    platform = router.detect(body.url) if body.url else _article_platform(router)
    initial_path = router.initial_path(platform)

    # 3. Resolve/create the platform folder, create stub doc.
    folder_id = await filer.resolve_or_create_folder(initial_path)
    title = body.shared_title or (body.url or "captured note")
    created = await filer._mcp.create_doc(title)
    doc_id = str(created["docId"])
    await filer._mcp.move_document(doc_id, folder_id=folder_id)
    await filer._mcp.append_blocks(
        doc_id,
        [{"type": "paragraph", "text": f"> Capturing... ({datetime.now(timezone.utc).isoformat()})"}],
    )

    web_url = _build_web_url(doc_id)
    capture_id = str(ULID())

    # 4. Insert capture row.
    row = CaptureRow(
        id=capture_id,
        url=body.url,
        url_hash=hash_value,
        source_app=body.source_app,
        shared_title=body.shared_title,
        shared_text=body.shared_text,
        platform=platform.id,
        status=CaptureStatus.QUEUED.value,
        doc_id=doc_id,
        web_url=web_url,
        topic_path="/".join(initial_path),
    )
    await repo.insert(row)

    return _row_to_response(row, router)


# ── Helpers ───────────────────────────────────────────────────────────


def _row_to_response(row: CaptureRow, router: PlatformRouter) -> CaptureResponse:
    return CaptureResponse(
        capture_id=row.id,
        doc_id=row.doc_id or "",
        web_url=row.web_url or "",
        status=CaptureStatus(row.status),
        platform=row.platform,
        initial_path=row.topic_path or "",
        created_at=row.created_at,
    )


def _article_platform(router: PlatformRouter):
    """Used when capture has shared_text but no URL."""
    # detect("") would raise; just look up the catch-all directly.
    for plat in router._platforms:  # noqa: SLF001 — internal but acceptable for v1
        if "*" in plat.hosts:
            return plat
    raise HTTPException(status_code=503, detail="No catch-all platform configured")


def _build_web_url(doc_id: str) -> str:
    base = os.environ.get("AFFINE_SERVER_EXTERNAL_URL", "http://localhost:3010")
    workspace = os.environ.get("AFFINE_WORKSPACE_ID", "")
    if workspace:
        return f"{base.rstrip('/')}/workspace/{workspace}/{doc_id}"
    return f"{base.rstrip('/')}/{doc_id}"
