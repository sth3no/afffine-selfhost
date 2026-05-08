"""FastAPI app for the ingest service.

Phase 3 ships /health (Phase 1) and POST /capture. List/get/retry/delete
endpoints land in Phase 7. Worker loop in Phase 6.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

import asyncpg
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from ulid import ULID

from src import error_envelope
from src.auth import require_token
from src.config import load_topics, settings
from src.db import CaptureRepository, CaptureRow, create_pool
from src.logging_setup import trace_id_var
from src.mcp_client import MCPClient
from src.models import (
    CaptureDetail,
    CaptureItem,
    CaptureRequest,
    CaptureResponse,
    CapturesPage,
    CaptureStatus,
    normalized_url,
    url_hash,
)
from src.pipeline.filer import Filer
from src.pipeline.router import PlatformRouter
from src.worker import Worker

log = logging.getLogger(__name__)


# ── Application-scoped state ──────────────────────────────────────────


class AppState:
    """Mutable container set during lifespan startup."""
    pool: asyncpg.Pool | None = None
    mcp: MCPClient | None = None
    filer: Filer | None = None
    router: PlatformRouter | None = None
    worker: Worker | None = None
    worker_task: asyncio.Task | None = None


app_state = AppState()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup: load topics, open DB pool, prepare MCP client + filer.
    Shutdown: close everything."""
    from src.logging_setup import setup_logging
    setup_logging(level=os.environ.get("LOG_LEVEL", "INFO"))

    # Topics & router (load once; hot-reload deferred to a future phase).
    topics = load_topics()
    app_state.router = PlatformRouter(topics)

    # MCP client to mcp_ext (lazy connect — no network call here).
    mcp_url = os.environ.get("MCP_EXT_URL", "http://mcp_ext:3100")
    affine_token = os.environ.get("AFFINE_ACCESS_TOKEN", "").strip()
    # Fail loudly when the token is missing rather than booting and 5xx-ing
    # on every /capture. Symptom seen in prod: `httpx.LocalProtocolError:
    # Illegal header value b'Bearer '` on each outbound MCP call.
    if not affine_token:
        raise RuntimeError(
            "AFFINE_ACCESS_TOKEN is empty. Set it in the stack env "
            "(AFFiNE → Workspace Settings → Integration → MCP Server → "
            "Generate Token) and redeploy. Without it, every /capture "
            "fails on the synchronous mcp_ext call."
        )
    app_state.mcp = await MCPClient(mcp_url, affine_token).__aenter__()

    # asyncpg pool (skipped when DATABASE_URL points at the placeholder used
    # by `pip install` smoke tests on developer machines).
    if settings.database_url and "placeholder" not in settings.database_url:
        app_state.pool = await create_pool(settings.database_url)

    # Filer needs the pool + an embed function for topic-folder dedup
    # (Phase 5). Without these, every confident-classified capture
    # raises "move_to_topic_folder requires embeddings_repo, ..." at
    # filing step.
    from src.pipeline.embeddings import embed
    app_state.filer = Filer(
        app_state.mcp,
        pool=app_state.pool,
        embed_fn=embed,
        similarity_threshold=settings.similarity_threshold,
    )

    # Crash recovery: reset any in-flight rows from a prior process restart.
    if app_state.pool is not None:
        async with app_state.pool.acquire() as conn:
            n = await CaptureRepository(conn).reset_in_flight_to_queued()
            if n > 0:
                log.info("crash recovery: reset %d in-flight rows to queued", n)

    # Start the worker (only when pool, filer, and router are available).
    if app_state.pool is not None and app_state.filer is not None and app_state.router is not None:
        from src.pipeline.orchestrator import process_capture
        from src.pipeline.classifier import classify
        from src.pipeline.extractors import get_extractor

        async def _process_fn(row, **kwargs):
            extractor = get_extractor(kwargs["platform"].extractor)
            await process_capture(
                row,
                platform=kwargs["platform"],
                topics=kwargs["topics"],
                repo=kwargs["repo"],
                filer=app_state.filer,
                extract_fn=extractor,
                classify_fn=classify,
            )

        def _platform_for(row):
            for p in app_state.router._platforms:
                if p.id == row.platform:
                    return p
            return app_state.router.catch_all  # fallback

        app_state.worker = Worker(
            pool=app_state.pool,
            repo_factory=lambda conn: CaptureRepository(conn),
            process_fn=_process_fn,
            platform_for=_platform_for,
            topics=load_topics(),
        )
        app_state.worker_task = asyncio.create_task(app_state.worker._loop())

    yield

    # Shutdown: stop worker first, then close pool and MCP.
    if app_state.worker is not None:
        app_state.worker.stop()
    if app_state.worker_task is not None:
        try:
            await asyncio.wait_for(app_state.worker_task, timeout=5.0)
        except asyncio.TimeoutError:
            app_state.worker_task.cancel()
    if app_state.pool is not None:
        await app_state.pool.close()
    if app_state.mcp is not None:
        await app_state.mcp.__aexit__(None, None, None)


app = FastAPI(title="affine-ingest", version=settings.version, lifespan=lifespan)
error_envelope.register(app)


@app.middleware("http")
async def trace_id_middleware(request: Request, call_next):
    """Stamp every request with a ULID trace_id.

    The id propagates into log records via the contextvar in logging_setup,
    is echoed back in the X-Trace-Id response header, and is included in
    error envelopes so the iOS Diagnostics dump can be greppped against
    server logs.
    """
    tid = str(ULID())
    token = trace_id_var.set(tid)
    try:
        response = await call_next(request)
        response.headers["X-Trace-Id"] = tid
        return response
    finally:
        trace_id_var.reset(token)


# ── DI providers (overrideable in tests) ──────────────────────────────


async def get_pool() -> asyncpg.Pool:
    if app_state.pool is None:
        raise HTTPException(status_code=503, detail="Database pool not initialized")
    return app_state.pool


async def get_capture_repo(
    pool: asyncpg.Pool = Depends(get_pool),
) -> AsyncIterator[CaptureRepository]:
    """Acquire a connection per request. Pool returns it on context exit."""
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
    queue_depth = 0
    if app_state.pool is not None:
        async with app_state.pool.acquire() as conn:
            queue_depth = await CaptureRepository(conn).count_active()
    return {
        "ok": True,
        "queue_depth": queue_depth,
        "worker_alive": bool(app_state.worker and app_state.worker.alive),
        "version": settings.version,
    }


@app.get("/health/deep")
async def health_deep() -> JSONResponse:
    """Probe every dependency that synchronous /capture exercises.

    Shallow /health is fine for `docker healthcheck` — it stays green as long
    as the process is up and the DB pool is alive. /health/deep additionally
    probes mcp_ext (TCP + /health) and AFFiNE-via-mcp_ext (a cheap MCP call),
    because /capture fails when either of those is broken even though the
    worker is "alive".

    Returns 200 when all checks pass, 503 otherwise. The payload always names
    each check + its latency so the operator can see which layer is red.
    """
    import time

    checks: dict[str, dict] = {}
    overall_ok = True

    # 1. DB pool reachable.
    t0 = time.monotonic()
    if app_state.pool is None:
        checks["db"] = {"ok": False, "error": "pool not initialized"}
        overall_ok = False
    else:
        try:
            async with app_state.pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            checks["db"] = {"ok": True, "latency_ms": int((time.monotonic() - t0) * 1000)}
        except Exception as e:
            checks["db"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            overall_ok = False

    # 2. mcp_ext liveness — TCP + /health.
    mcp_url = os.environ.get("MCP_EXT_URL", "http://mcp_ext:3100").rstrip("/")
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{mcp_url}/health")
        if resp.status_code == 200:
            checks["mcp_ext"] = {
                "ok": True,
                "latency_ms": int((time.monotonic() - t0) * 1000),
            }
        else:
            checks["mcp_ext"] = {
                "ok": False,
                "status_code": resp.status_code,
                "latency_ms": int((time.monotonic() - t0) * 1000),
            }
            overall_ok = False
    except Exception as e:
        checks["mcp_ext"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        overall_ok = False

    # 3. AFFiNE-via-mcp_ext — exercises AFFINE_ACCESS_TOKEN + workspace.
    #    Skipped automatically when mcp_ext probe is already red.
    if checks.get("mcp_ext", {}).get("ok") and app_state.mcp is not None:
        t0 = time.monotonic()
        try:
            await app_state.mcp.list_folder_tree()
            checks["mcp_affine"] = {
                "ok": True,
                "latency_ms": int((time.monotonic() - t0) * 1000),
            }
        except Exception as e:
            checks["mcp_affine"] = {
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "latency_ms": int((time.monotonic() - t0) * 1000),
            }
            overall_ok = False

    return JSONResponse(
        status_code=200 if overall_ok else 503,
        content={
            "ok": overall_ok,
            "checks": checks,
            "worker_alive": bool(app_state.worker and app_state.worker.alive),
            "version": settings.version,
        },
    )


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


# ── Constants ─────────────────────────────────────────────────────────

MAX_LIST_LIMIT = 200
DEFAULT_LIST_LIMIT = 50


# ── Routes ── Phase 7: read + manage ─────────────────────────────────


@app.get("/captures", response_model=CapturesPage)
async def list_captures(
    limit: int = DEFAULT_LIST_LIMIT,
    status: str | None = None,
    platform: str | None = None,
    repo: CaptureRepository = Depends(get_capture_repo),
    _: str = require_token,
) -> CapturesPage:
    limit = max(1, min(limit, MAX_LIST_LIMIT))
    rows = await repo.list_captures(limit=limit, status=status, platform=platform)
    items = [_row_to_item(r) for r in rows]
    return CapturesPage(items=items, next_cursor=None)


@app.get("/captures/{capture_id}", response_model=CaptureDetail)
async def get_capture(
    capture_id: str,
    repo: CaptureRepository = Depends(get_capture_repo),
    _: str = require_token,
) -> CaptureDetail:
    row = await repo.get_by_id(capture_id)
    if row is None or row.status == "deleted":
        raise HTTPException(status_code=404, detail="Capture not found")
    return _row_to_detail(row)


_IN_FLIGHT_STATUSES = {"queued", "extracting", "classifying", "filing"}


@app.post(
    "/captures/{capture_id}/retry",
    response_model=CaptureResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_capture(
    capture_id: str,
    repo: CaptureRepository = Depends(get_capture_repo),
    _: str = require_token,
) -> CaptureResponse:
    existing = await repo.get_by_id(capture_id)
    if existing is None or existing.status == "deleted":
        raise HTTPException(status_code=404, detail="Capture not found")
    if existing.status in _IN_FLIGHT_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Capture is already in flight (status={existing.status})",
        )
    row = await repo.mark_for_retry(capture_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Capture not found")
    return _row_to_response(row, None)  # type: ignore[arg-type]  # router unused in helper


@app.delete("/captures/{capture_id}")
async def delete_capture(
    capture_id: str,
    repo: CaptureRepository = Depends(get_capture_repo),
    filer: Filer = Depends(get_filer),
    _: str = require_token,
) -> dict:
    row = await repo.mark_deleted(capture_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Capture not found")
    if row.doc_id:
        try:
            await filer._mcp.delete_doc(row.doc_id)
        except Exception as e:
            # Doc may already be trashed in AFFiNE; log + continue. The row is
            # marked deleted regardless.
            log.warning(
                "delete_doc failed for capture %s doc %s: %s",
                capture_id, row.doc_id, e,
            )
    return {"ok": True}


# ── Routes ── Phase 12: YouTube cookies ──────────────────────────────


@app.post("/youtube/cookies", status_code=status.HTTP_204_NO_CONTENT)
async def upload_youtube_cookies(
    request: Request,
    _: str = require_token,
):
    """Upload a Netscape-format cookies.txt for YouTube extractors.

    The browser extension POSTs this with the user's current YT session
    cookies. We write atomically to a tmpfs file (chmod 600), and yt-dlp
    + youtube-transcript-api + cobalt all read from there on every
    request.

    NEVER logs the body. Only logs `byte_count` for ops visibility.
    """
    from pathlib import Path

    from src.youtube_cookies import (
        InvalidCookieFile,  # noqa: F401 — kept available for tests
        validate_netscape,
        write_cookies_atomic,
    )

    raw = (await request.body()).decode("utf-8", errors="replace")
    ok, err = validate_netscape(raw)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid cookies.txt: {err}",
        )

    write_cookies_atomic(raw, Path(settings.youtube_cookies_path))
    log.info(
        "youtube cookies uploaded",
        extra={"byte_count": len(raw), "path": settings.youtube_cookies_path},
    )


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
    """Used when capture has shared_text but no URL — falls back to the
    catch-all platform (typically `article` with hosts: ["*"]).
    """
    plat = router.catch_all
    if plat is None:
        raise HTTPException(status_code=503, detail="No catch-all platform configured")
    return plat


def _row_to_item(row: CaptureRow, *, completed_at: "datetime | None" = None) -> CaptureItem:
    return CaptureItem(
        capture_id=row.id,
        url=row.url,
        platform=row.platform,
        status=CaptureStatus(row.status),
        doc_id=row.doc_id,
        web_url=row.web_url,
        topic_path=row.topic_path,
        created_at=row.created_at,
        completed_at=completed_at,
    )


def _row_to_detail(row: CaptureRow) -> CaptureDetail:
    return CaptureDetail(
        capture_id=row.id,
        url=row.url,
        platform=row.platform,
        status=CaptureStatus(row.status),
        doc_id=row.doc_id,
        web_url=row.web_url,
        topic_path=row.topic_path,
        created_at=row.created_at,
        completed_at=None,  # Phase 6 schema has completed_at; fetch later
        error=None,         # error column read in mark_failed but not exposed in CaptureRow yet
        retry_count=row.retry_count,
        classifier_reasoning=row.classifier_reasoning,
    )


def _build_web_url(doc_id: str) -> str:
    """Construct the AFFiNE workspace doc URL from settings.

    Logs a warning (does not raise) when AFFINE_WORKSPACE_ID is empty —
    the URL is non-functional without a workspace, but we want the API
    to keep responding so iOS can record the capture and the operator
    can fix the missing env. Phase 9 hardens this into a startup check.
    """
    base = settings.affine_server_external_url
    workspace = settings.affine_workspace_id
    if not workspace:
        # Non-fatal at request time; flag for the operator.
        return f"{base.rstrip('/')}/{doc_id}"
    return f"{base.rstrip('/')}/workspace/{workspace}/{doc_id}"
