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
    ContentTemplateView,
    CreateTemplateRequest,
    SynthesizeRequest,
    UpdateTemplateRequest,
    normalized_url,
    url_hash,
)
from src.pipeline.extracted import from_snapshot
from src.pipeline.template_synth import synthesize_template
from src.pipeline.templates import ContentTemplate, TemplatesRepository
from src.pipeline.templated_render import render as templated_render
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
    worker_tasks: list[asyncio.Task] = []
    templates_repo: TemplatesRepository | None = None


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

    if app_state.pool is not None:
        from src.pipeline.templates import TemplatesRepository
        app_state.templates_repo = TemplatesRepository(app_state.pool)

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

    # Start the worker (only when pool, filer, router, and templates_repo are available).
    # All four conditions must hold to start the worker. templates_repo is
    # initialized together with pool, so it's never None when pool is set;
    # the explicit check is defensive against future lifespan refactors.
    if app_state.pool is not None and app_state.filer is not None \
           and app_state.router is not None and app_state.templates_repo is not None:
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
                templates_repo=app_state.templates_repo,
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
            topics=topics,
            capture_timeout_sec=float(settings.capture_timeout_sec),
        )
        n_loops = max(1, settings.worker_concurrency)
        app_state.worker_tasks = [
            asyncio.create_task(app_state.worker._loop()) for _ in range(n_loops)
        ]

    yield

    # Shutdown: stop worker loops first, then close pool and MCP.
    if app_state.worker is not None:
        app_state.worker.stop()
    if app_state.worker_tasks:
        _, pending = await asyncio.wait(app_state.worker_tasks, timeout=5.0)
        for t in pending:
            t.cancel()
        app_state.worker_tasks = []
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
async def health() -> JSONResponse:
    """Liveness check. Returns 503 when the worker was started but its loop
    has died — the compose healthcheck tests for HTTP 200, so a dead worker
    now gets the container restarted instead of silently queueing captures
    forever. When no worker was started at all (dev mode without a DB pool),
    the service is still "ok" — there is nothing to pump."""
    queue_depth = 0
    if app_state.pool is not None:
        async with app_state.pool.acquire() as conn:
            queue_depth = await CaptureRepository(conn).count_active()
    worker_started = app_state.worker is not None
    worker_alive = bool(app_state.worker and app_state.worker.alive)
    ok = worker_alive or not worker_started
    return JSONResponse(
        status_code=200 if ok else 503,
        content={
            "ok": ok,
            "queue_depth": queue_depth,
            "worker_alive": worker_alive,
            "version": settings.version,
        },
    )


@app.get("/diagnostic/logging")
async def diagnostic_logging() -> dict:
    """Snapshot of the logging-handler topology.

    Use this when production logs show the mangled `INFO INFO INFO ts=...`
    pattern: a healthy response has `json_formatter_active=true`,
    `root_handler_count=1`, and `extra_handler_loggers=[]`. If any of
    those are wrong, the in-memory state has drifted from setup_logging
    — likely a re-attached handler from a framework. If they all look
    right but logs still look mangled, the issue is in the display layer
    (e.g. Portainer's log viewer rendering JSON as logfmt) and not the
    code.
    """
    from src.logging_setup import audit_log_handlers
    return audit_log_handlers()


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

    # Skip the worker's idle poll delay — pickup is immediate.
    if app_state.worker is not None:
        app_state.worker.wake()

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


# ── Routes ── Phase 14: Templates CRUD + ops ─────────────────────────


def _template_to_view(t: ContentTemplate, usage_count: int) -> ContentTemplateView:
    return ContentTemplateView(
        id=t.id, platform_id=t.platform_id, topic=t.topic,
        name=t.name, system_prompt=t.system_prompt, status=t.status,
        generator_meta=t.generator_meta, created_by=t.created_by,
        created_at=t.created_at, updated_at=t.updated_at,
        usage_count=usage_count,
    )


def _require_templates_repo() -> TemplatesRepository:
    repo = app_state.templates_repo
    if repo is None:
        raise HTTPException(status_code=503, detail="templates_repo not initialized")
    return repo


@app.get("/templates", response_model=list[ContentTemplateView])
async def list_templates(
    platform: str | None = None,
    topic: str | None = None,
    status_filter: str | None = None,
    _: str = require_token,
):
    repo = _require_templates_repo()
    rows = await repo.list_all(platform_id=platform, topic=topic, status=status_filter)
    return [_template_to_view(t, await repo.count_usage(template_id=t.id)) for t in rows]


@app.get("/templates/resolve", response_model=ContentTemplateView)
async def resolve_template(
    platform: str, topic: str, _: str = require_token,
):
    repo = _require_templates_repo()
    t = await repo.resolve(platform_id=platform, topic=topic)
    if t is None:
        raise HTTPException(status_code=404, detail="no template matches")
    return _template_to_view(t, await repo.count_usage(template_id=t.id))


@app.get("/templates/{template_id}", response_model=ContentTemplateView)
async def get_template(template_id: str, _: str = require_token):
    repo = _require_templates_repo()
    t = await repo.get(template_id=template_id)
    if t is None:
        raise HTTPException(status_code=404)
    return _template_to_view(t, await repo.count_usage(template_id=t.id))


@app.post("/templates", response_model=ContentTemplateView, status_code=201)
async def create_template(
    body: CreateTemplateRequest, _: str = require_token,
):
    repo = _require_templates_repo()
    try:
        t = await repo.create(
            platform_id=body.platform_id,
            topic=body.topic,
            name=body.name,
            system_prompt=body.system_prompt,
            status="edited",
            created_by="user",
            generator_meta=None,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail="A template already exists for this (platform_id, topic) scope.",
        )
    return _template_to_view(t, 0)


@app.put("/templates/{template_id}", response_model=ContentTemplateView)
async def update_template(
    template_id: str, body: UpdateTemplateRequest, _: str = require_token,
):
    repo = _require_templates_repo()
    try:
        t = await repo.update(
            template_id=template_id,
            name=body.name,
            system_prompt=body.system_prompt,
            platform_id=body.platform_id,
            topic=body.topic,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail="Scope change collides with an existing active template.",
        )
    if t is None:
        raise HTTPException(status_code=404)
    return _template_to_view(t, await repo.count_usage(template_id=t.id))


@app.delete("/templates/{template_id}", response_model=ContentTemplateView)
async def archive_template(template_id: str, _: str = require_token):
    repo = _require_templates_repo()
    target = await repo.get(template_id=template_id)
    if target is None:
        raise HTTPException(status_code=404)

    # Seed protection: don't allow deleting the only active (*, *) row.
    if target.platform_id == "*" and target.topic == "*" and target.status != "archived":
        actives = await repo.list_all(platform_id="*", topic="*")
        non_archived = [t for t in actives if t.status != "archived"]
        if len(non_archived) <= 1:
            raise HTTPException(
                status_code=409,
                detail="Refusing to archive the only active (*, *) seed template. "
                       "POST a replacement first.",
            )

    archived = await repo.archive(template_id=template_id)
    if archived is None:
        raise HTTPException(
            status_code=404,
            detail="Template disappeared between get and archive (concurrent delete).",
        )
    return _template_to_view(archived, await repo.count_usage(template_id=archived.id))


@app.post("/templates/synthesize", response_model=ContentTemplateView, status_code=201)
async def synth_endpoint(
    body: SynthesizeRequest, _: str = require_token,
):
    repo = _require_templates_repo()
    existing = await repo.resolve(platform_id=body.platform_id, topic=body.topic)
    if existing is not None and existing.platform_id == body.platform_id and existing.topic == body.topic:
        raise HTTPException(
            status_code=409,
            detail="An active template already exists at this scope. "
                   "DELETE it first to regenerate.",
        )

    sample_extracted = await _load_sample_extracted(
        sample_capture_id=body.sample_capture_id,
        platform_id=body.platform_id,
        topic=body.topic,
    )
    if sample_extracted is None:
        raise HTTPException(
            status_code=400,
            detail="No sample capture available for synthesis. Either provide "
                   "sample_capture_id or wait until at least one capture lands "
                   "in this (platform, topic) scope.",
        )

    tmpl = await synthesize_template(
        platform_id=body.platform_id,
        topic=body.topic,
        sample_extracted=sample_extracted,
        templates_repo=repo,
    )
    return _template_to_view(tmpl, 0)


async def _load_sample_extracted(
    *,
    sample_capture_id: str | None,
    platform_id: str,
    topic: str,
):
    """Load the most recent capture's extracted_snapshot for the given scope,
    or the specific row if sample_capture_id is provided. Returns an Extracted
    record or None."""
    if app_state.pool is None:
        return None
    async with app_state.pool.acquire() as conn:
        if sample_capture_id is not None:
            row = await conn.fetchrow(
                "SELECT extracted_snapshot FROM captures WHERE id = $1",
                sample_capture_id,
            )
        else:
            row = await conn.fetchrow(
                """
                SELECT extracted_snapshot FROM captures
                WHERE platform = $1 AND classifier_topic = $2
                  AND extracted_snapshot IS NOT NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                platform_id, topic,
            )
    if row is None or row["extracted_snapshot"] is None:
        return None
    return from_snapshot(row["extracted_snapshot"])


@app.post("/captures/{capture_id}/rerender", response_model=CaptureDetail)
async def rerender_capture(
    capture_id: str,
    reextract: bool = False,
    _: str = require_token,
):
    """Re-run the currently-resolved template against the capture's stored
    extracted_snapshot.

    With ?reextract=true, fall back to re-fetching the URL if no snapshot
    exists (older pre-template captures). Not yet implemented in v1.

    v1 caveats:
    - **Append-only.** Blocks are appended to the existing doc body — calling
      this multiple times for the same capture will accumulate duplicate
      Summary / body sections. Replace/diff semantics are planned for v2.
    - **No concurrency lock.** Two simultaneous rerenders of the same capture
      will both succeed; blocks will be duplicated in the AFFiNE doc. For
      single-user self-hosted deployments this is acceptable.
    """
    repo_t = _require_templates_repo()
    if app_state.pool is None:
        raise HTTPException(status_code=503, detail="DB pool not initialized")

    async with app_state.pool.acquire() as conn:
        captures_repo = CaptureRepository(conn)
        row = await captures_repo.get_by_id(capture_id)
        if row is None:
            raise HTTPException(status_code=404)

        snapshot = row.extracted_snapshot
        if snapshot is None and not reextract:
            raise HTTPException(
                status_code=400,
                detail="No extracted_snapshot for this capture. Pass "
                       "?reextract=true to refetch the source URL.",
            )

        if snapshot is None:
            raise HTTPException(
                status_code=501,
                detail=(
                    "reextract=true is not yet supported. Captures processed before "
                    "Phase 14 (no extracted_snapshot) cannot be rerendered. Workaround: "
                    "POST /capture again with the original URL to create a new processed "
                    "capture, then rerender that."
                ),
            )

        extracted = from_snapshot(snapshot)

        # Resolve current template for this capture's (platform, topic).
        topic = getattr(row, "classifier_topic", None) or "*"
        template = await repo_t.resolve(platform_id=row.platform, topic=topic)
        if template is None:
            template = await repo_t.resolve(platform_id="*", topic="*")
            if template is None:
                raise HTTPException(
                    status_code=503,
                    detail="No template available — seed (*, *) is missing.",
                )

        # Run the render.
        keyframes = (extracted.extra or {}).get("keyframes") or []
        rendered = await templated_render(
            template=template, extracted=extracted, keyframes=keyframes,
        )

        # Persist the new template_run.
        await captures_repo.save_template_run(
            capture_id=row.id,
            template_id=template.id,
            prompt_used=template.system_prompt,
            output_raw=rendered.body_md,
        )

        # Replace blocks in the AFFiNE doc.
        if app_state.mcp is None or row.doc_id is None:
            log.warning(
                "rerender: MCP unavailable or doc_id missing; skipped block update"
            )
        else:
            from src.pipeline.markdown_render import count_keyframe_refs, markdown_to_blocks
            from src.pipeline.orchestrator import url_embed_block
            blocks: list[dict] = []
            if row.url:
                blocks.append(url_embed_block(row.url))
            if rendered.lede and rendered.lede.strip():
                blocks.append({"type": "callout", "text": rendered.lede.strip()})
            if rendered.summary_md:
                blocks.append({"type": "paragraph", "style": "h2", "text": "Summary"})
                blocks.extend(
                    await markdown_to_blocks(
                        rendered.summary_md,
                        keyframes=keyframes,
                        mcp_client=app_state.mcp,
                    )
                )
            if rendered.body_md:
                blocks.extend(
                    await markdown_to_blocks(
                        rendered.body_md,
                        keyframes=keyframes,
                        mcp_client=app_state.mcp,
                    )
                )

            # Phase 15 fallback: append ## Keyframes when body_md referenced
            # zero kf:N refs out of N available keyframes. Mirrors the
            # orchestrator's behaviour in _replace_doc_body_templated.
            if (
                keyframes
                and rendered.body_md
                and not count_keyframe_refs(rendered.body_md)
            ):
                blocks.append({"type": "paragraph", "style": "h2", "text": "Keyframes"})
                for kf in keyframes:
                    source_id = kf.get("blob_source_id")
                    if not source_id:
                        continue
                    blocks.append({
                        "type": "image",
                        "sourceId": source_id,
                        "caption": kf.get("caption") or "",
                    })

            # Always append the raw transcript/body as a separate section so
            # the source signal is preserved even when the template's body_md
            # is a compressed summary. Strip extractor metadata first so we
            # don't get duplicate Title/Source/## Transcript blocks.
            if extracted.body_md and extracted.body_md.strip():
                from src.pipeline.orchestrator import strip_extractor_metadata
                transcript_md = strip_extractor_metadata(extracted.body_md)
                if transcript_md.strip():
                    blocks.append({"type": "paragraph", "style": "h2", "text": "Transcript"})
                    blocks.extend(
                        await markdown_to_blocks(
                            transcript_md,
                            keyframes=keyframes,
                            mcp_client=app_state.mcp,
                        )
                    )
            if row.url:
                blocks.append({
                    "type": "paragraph", "style": "text",
                    "text": [
                        {"text": "Source: "},
                        {"text": row.url, "italic": True, "link": row.url},
                    ],
                })
            # Naive: append blocks after existing ones (v2 will diff/replace).
            await app_state.mcp.append_blocks(row.doc_id, blocks)

        # Refetch + return CaptureDetail.
        refreshed = await captures_repo.get_by_id(capture_id)
        if refreshed is None:
            raise HTTPException(status_code=404)
        return _row_to_detail(refreshed)


# ── Routes ── Phase 12: YouTube cookies ──────────────────────────────


@app.post("/youtube/cookies", status_code=status.HTTP_204_NO_CONTENT)
async def upload_youtube_cookies(
    request: Request,
    _: str = require_token,
):
    """Upload a Netscape-format cookies.txt for YouTube extractors.

    The browser extension POSTs this with the user's current YT session
    cookies. We write TWO files atomically to a tmpfs (chmod 600):
      - youtube.txt (Netscape) — for yt-dlp `--cookies` and transcript-api
      - cobalt.json (cobalt v11 format) — cobalt does NOT read Netscape

    NEVER logs the body. Only logs `byte_count` for ops visibility.
    """
    from pathlib import Path

    from src.youtube_cookies import (
        InvalidCookieFile,  # noqa: F401 — kept available for tests
        netscape_to_cobalt_json,
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

    netscape_path = Path(settings.youtube_cookies_path)
    write_cookies_atomic(raw, netscape_path)

    # Cobalt v11 needs JSON. Write the converted file alongside the Netscape one.
    cobalt_path = netscape_path.parent / "cobalt.json"
    cobalt_body = netscape_to_cobalt_json(raw)
    write_cookies_atomic(cobalt_body, cobalt_path)

    log.info(
        "youtube cookies uploaded",
        extra={
            "byte_count": len(raw),
            "cobalt_byte_count": len(cobalt_body),
            "path": settings.youtube_cookies_path,
        },
    )


@app.get("/youtube/cookies/status")
async def get_youtube_cookies_status(_: str = require_token):
    """Read-only freshness check. NEVER returns cookie content.

    The browser extension polls this on its daily alarm to detect the
    "ingest container restarted, tmpfs is empty, my browser-side lastSync
    is misleading" failure mode. The extension renders a stale/missing
    badge based on `age_seconds`.
    """
    from pathlib import Path

    from src.youtube_cookies import cookie_file_status

    return cookie_file_status(Path(settings.youtube_cookies_path))


@app.get("/youtube/cookies/diagnostic")
async def get_youtube_cookies_diagnostic(_: str = require_token):
    """Diagnostic snapshot — cookie NAMES + structural metadata only.

    NEVER returns cookie values. Used to verify the right auth cookie
    names (`__Secure-3PSID`, `SID`, etc.) are present in both the
    Netscape file and the cobalt JSON. If they're missing, the upstream
    sync from the browser extension is incomplete; if they're present
    but auth still fails, the issue is downstream (cookies invalid /
    YT IP block / yt-dlp PO token requirement / etc.).
    """
    from pathlib import Path

    from src.youtube_cookies import (
        cobalt_json_diagnostic,
        cookie_file_status,
        cookie_names_only,
    )

    netscape_path = Path(settings.youtube_cookies_path)
    cobalt_path = netscape_path.parent / "cobalt.json"

    result: dict = {
        "netscape": {
            **cookie_file_status(netscape_path),
            "cookies": [],
        },
        "cobalt": {
            **cookie_file_status(cobalt_path),
            "parse": {"valid_json": False, "services": {}},
        },
    }

    if netscape_path.is_file():
        try:
            content = netscape_path.read_text(encoding="utf-8", errors="replace")
            result["netscape"]["cookies"] = cookie_names_only(content)
        except OSError as e:
            result["netscape"]["read_error"] = str(e)

    if cobalt_path.is_file():
        try:
            content = cobalt_path.read_text(encoding="utf-8", errors="replace")
            result["cobalt"]["parse"] = cobalt_json_diagnostic(content)
        except OSError as e:
            result["cobalt"]["read_error"] = str(e)

    return result


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


def _row_to_item(row: CaptureRow) -> CaptureItem:
    return CaptureItem(
        capture_id=row.id,
        url=row.url,
        platform=row.platform,
        status=CaptureStatus(row.status),
        doc_id=row.doc_id,
        web_url=row.web_url,
        topic_path=row.topic_path,
        created_at=row.created_at,
        completed_at=row.completed_at,
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
        completed_at=row.completed_at,
        error=row.error,
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
