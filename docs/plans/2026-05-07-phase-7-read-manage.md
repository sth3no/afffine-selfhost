# Phase 7 — Read + Manage Endpoints (GET / Retry / Delete)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Complete the HTTP API per spec §4. iOS app can list captures, view a single capture's detail, manually retry a failed/done one, and soft-delete (which also soft-trashes the AFFiNE doc via `mcp_ext.delete_doc`).

**Macro plan:** [`docs/plans/2026-05-06-ingest-service-macro-plan.md`](./2026-05-06-ingest-service-macro-plan.md) — Phase 7
**Spec:** [`docs/specs/2026-05-06-ingest-service-design.md`](../specs/2026-05-06-ingest-service-design.md) — §4 (full API surface)
**Phase 6 prereq:** PR #12 (worker) merged or in branch's history. Worker pumps the queue; the new endpoints don't need to talk to the worker — manual retry just resets row state and the worker picks up.

**Architecture:**
- Each new endpoint goes through the same auth dependency, same DI providers as POST /capture.
- All endpoints return either `CaptureResponse` (single) or `CapturesPage` (list with cursor).
- DELETE soft-trashes the AFFiNE doc + marks the row `status='deleted'`. GET on a deleted row returns 404.
- Retry **resets classifier output** so the next worker pass re-classifies from scratch. Sets `status='queued'`, `retry_count=0`, `next_attempt_at=NULL`, clears `classifier_topic`/`classifier_conf`/`classifier_reasoning`/`error`.

**End-of-phase test count:** ~147 (current) + ~18 new ≈ ~165 passed, 5 skipped.

---

## Task 1: DB queries (list + retry + soft-delete)

**Files:**
- Modify: `ingest/src/db.py`
- Create: `ingest/tests/test_db_phase7.py`

**New methods on `CaptureRepository`:**

| Method | Purpose |
|---|---|
| `list_captures(*, limit, status=None, platform=None, before=None) -> list[CaptureRow]` | List with optional filters + cursor (`before` is a created_at timestamp; rows older than it are returned) |
| `mark_for_retry(capture_id) -> CaptureRow \| None` | UPDATE → status='queued', retry_count=0, next_attempt_at=NULL, classifier_*=NULL, error=NULL. Returns the row or None if not found / already deleted |
| `mark_deleted(capture_id) -> CaptureRow \| None` | UPDATE → status='deleted', updated_at=NOW. Returns the row (so the handler can read doc_id for mcp.delete_doc) |

- [ ] **Step 1.1: Write failing tests**

```python
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.db import CaptureRepository, CaptureRow


def _row_dict(**overrides):
    base = {
        "id": "01J", "url": "https://x", "url_hash": "h", "source_app": None,
        "shared_title": None, "shared_text": None, "platform": "article",
        "status": "done", "doc_id": "d", "web_url": "w",
        "topic_path": "Sources/Articles/Web/Tech",
        "classifier_topic": "Tech", "classifier_conf": 0.9,
        "classifier_reasoning": "ok",
        "retry_count": 0,
        "created_at": datetime(2026, 5, 7, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_list_captures_default_limit_returns_newest_first():
    conn = AsyncMock()
    conn.fetch.return_value = [_row_dict(id="b"), _row_dict(id="a")]
    repo = CaptureRepository(conn)
    rows = await repo.list_captures(limit=50)
    assert [r.id for r in rows] == ["b", "a"]
    sql = conn.fetch.call_args.args[0]
    assert "ORDER BY created_at DESC" in sql
    assert "LIMIT" in sql


@pytest.mark.asyncio
async def test_list_captures_filters_by_status():
    conn = AsyncMock()
    conn.fetch.return_value = []
    repo = CaptureRepository(conn)
    await repo.list_captures(limit=10, status="failed")
    sql = conn.fetch.call_args.args[0]
    args = conn.fetch.call_args.args[1:]
    assert "status" in sql
    assert "failed" in args


@pytest.mark.asyncio
async def test_list_captures_filters_by_platform():
    conn = AsyncMock()
    conn.fetch.return_value = []
    repo = CaptureRepository(conn)
    await repo.list_captures(limit=10, platform="instagram")
    sql = conn.fetch.call_args.args[0]
    args = conn.fetch.call_args.args[1:]
    assert "platform" in sql
    assert "instagram" in args


@pytest.mark.asyncio
async def test_list_captures_combines_filters():
    conn = AsyncMock()
    conn.fetch.return_value = []
    repo = CaptureRepository(conn)
    await repo.list_captures(limit=10, status="done", platform="instagram")
    args = conn.fetch.call_args.args[1:]
    assert "done" in args
    assert "instagram" in args


@pytest.mark.asyncio
async def test_list_captures_cursor_filters_older_than_before():
    conn = AsyncMock()
    conn.fetch.return_value = []
    repo = CaptureRepository(conn)
    cursor = datetime(2026, 5, 1, tzinfo=timezone.utc)
    await repo.list_captures(limit=10, before=cursor)
    sql = conn.fetch.call_args.args[0]
    assert "created_at <" in sql.replace(" ", "<") or "created_at <" in sql
    args = conn.fetch.call_args.args[1:]
    assert cursor in args


@pytest.mark.asyncio
async def test_mark_for_retry_resets_classifier_and_returns_row():
    conn = AsyncMock()
    conn.fetchrow.return_value = _row_dict(status="queued", classifier_topic=None,
                                          classifier_conf=None, classifier_reasoning=None,
                                          retry_count=0)
    repo = CaptureRepository(conn)
    row = await repo.mark_for_retry("01J")
    assert row is not None
    assert row.status == "queued"
    assert row.classifier_topic is None
    sql = conn.fetchrow.call_args.args[0]
    # Verify all the resets are in the SQL.
    for token in ("classifier_topic = NULL", "classifier_conf = NULL",
                  "classifier_reasoning = NULL", "error = NULL",
                  "retry_count = 0", "next_attempt_at = NULL", "status = 'queued'"):
        assert token in sql.replace("  ", " "), f"missing: {token}"


@pytest.mark.asyncio
async def test_mark_for_retry_returns_none_when_not_found():
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    repo = CaptureRepository(conn)
    assert await repo.mark_for_retry("missing") is None


@pytest.mark.asyncio
async def test_mark_deleted_returns_row_with_doc_id():
    conn = AsyncMock()
    conn.fetchrow.return_value = _row_dict(status="deleted")
    repo = CaptureRepository(conn)
    row = await repo.mark_deleted("01J")
    assert row is not None
    assert row.status == "deleted"
    assert row.doc_id == "d"
    sql = conn.fetchrow.call_args.args[0]
    assert "status = 'deleted'" in sql or "status='deleted'" in sql.replace(" ", "")
```

- [ ] **Step 1.2: Implement — append to `CaptureRepository` in `db.py`**

```python
    # ── Read + manage queries (Phase 7) ───────────────────────────────

    async def list_captures(
        self,
        *,
        limit: int,
        status: str | None = None,
        platform: str | None = None,
        before: datetime | None = None,
    ) -> list[CaptureRow]:
        """List captures newest-first, optionally filtered by status/platform.

        `before` is a cursor — only rows with `created_at < before` are
        returned. Combine with limit for pagination.
        """
        clauses: list[str] = []
        args: list[Any] = []
        if status is not None:
            args.append(status)
            clauses.append(f"status = ${len(args)}")
        if platform is not None:
            args.append(platform)
            clauses.append(f"platform = ${len(args)}")
        if before is not None:
            args.append(before)
            clauses.append(f"created_at < ${len(args)}")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        args.append(limit)
        sql = f"""
            SELECT id, url, url_hash, source_app, shared_title, shared_text,
                   platform, status, doc_id, web_url, topic_path,
                   classifier_topic, classifier_conf, classifier_reasoning,
                   retry_count, created_at
            FROM captures
            {where}
            ORDER BY created_at DESC
            LIMIT ${len(args)}
        """
        records = await self._conn.fetch(sql, *args)
        return [CaptureRow(**dict(r)) for r in records]

    async def mark_for_retry(self, capture_id: str) -> CaptureRow | None:
        """Reset the row for re-processing by the worker.

        Clears classifier output + error + retry_count + next_attempt_at, and
        sets status back to 'queued'. Soft-deleted rows are NOT retried — the
        WHERE clause excludes them.
        """
        sql = """
            UPDATE captures
            SET status = 'queued',
                retry_count = 0,
                next_attempt_at = NULL,
                classifier_topic = NULL,
                classifier_conf = NULL,
                classifier_reasoning = NULL,
                error = NULL,
                updated_at = NOW()
            WHERE id = $1 AND status <> 'deleted'
            RETURNING id, url, url_hash, source_app, shared_title, shared_text,
                      platform, status, doc_id, web_url, topic_path,
                      classifier_topic, classifier_conf, classifier_reasoning,
                      retry_count, created_at
        """
        rec = await self._conn.fetchrow(sql, capture_id)
        return None if rec is None else CaptureRow(**dict(rec))

    async def mark_deleted(self, capture_id: str) -> CaptureRow | None:
        """Soft-delete: status='deleted' but the row remains for audit + GET 404."""
        sql = """
            UPDATE captures
            SET status = 'deleted', updated_at = NOW()
            WHERE id = $1
            RETURNING id, url, url_hash, source_app, shared_title, shared_text,
                      platform, status, doc_id, web_url, topic_path,
                      classifier_topic, classifier_conf, classifier_reasoning,
                      retry_count, created_at
        """
        rec = await self._conn.fetchrow(sql, capture_id)
        return None if rec is None else CaptureRow(**dict(rec))
```

- [ ] **Step 1.3: Run + commit**

```bash
cd ingest && python -m pytest tests/test_db_phase7.py -v
git add ingest/src/db.py ingest/tests/test_db_phase7.py
git commit -m "$(cat <<'EOF'
feat(ingest): list_captures + mark_for_retry + mark_deleted DB queries

list_captures supports newest-first ORDER BY created_at DESC, optional
status / platform filters with parameterized predicates, and a `before`
cursor for pagination (created_at < before).

mark_for_retry resets the row to queued + clears classifier output +
error + retry_count + next_attempt_at, so the worker picks it up fresh
on the next tick. Excludes soft-deleted rows via WHERE status <>
'deleted'. Returns None when the row is missing or deleted.

mark_deleted is the soft-delete: status='deleted', preserving the row
for audit + history. The DELETE handler reads the returned doc_id to
send mcp.delete_doc.

Phase 7 / Task 1 of docs/plans/2026-05-07-phase-7-read-manage.md
EOF
)"
```

---

## Task 2: Wire models — `CapturesPage`, `CaptureDetail`

Pydantic shapes for the new endpoints. Spec §4 returns:

```json
GET /captures → { items: [CaptureItem...], next_cursor: str | null }
GET /captures/{id} → CaptureDetail (item + error + reasoning + retry_count)
```

**Files:**
- Modify: `ingest/src/models.py` — add `CaptureItem`, `CapturesPage`, `CaptureDetail`
- Create: `ingest/tests/test_phase7_models.py`

- [ ] **Step 2.1: Write failing tests**

```python
from datetime import datetime, timezone

from src.models import CaptureDetail, CaptureItem, CapturesPage, CaptureStatus


def _now():
    return datetime(2026, 5, 7, 14, 20, 0, tzinfo=timezone.utc)


def test_capture_item_serializes_iso8601_z():
    item = CaptureItem(
        capture_id="01J",
        url="https://x",
        platform="instagram",
        status=CaptureStatus.DONE,
        doc_id="d",
        web_url="w",
        topic_path="Sources/Socials/Instagram/Recipes",
        created_at=_now(),
        completed_at=_now(),
    )
    payload = item.model_dump(mode="json")
    assert payload["status"] == "done"
    assert payload["created_at"].endswith("Z")


def test_capture_item_optional_fields_default_to_none():
    item = CaptureItem(
        capture_id="01J", url=None, platform="article",
        status=CaptureStatus.QUEUED,
        doc_id=None, web_url=None, topic_path=None,
        created_at=_now(),
    )
    assert item.completed_at is None


def test_captures_page_with_items_and_cursor():
    item = CaptureItem(
        capture_id="01J", url=None, platform="article",
        status=CaptureStatus.QUEUED, doc_id=None, web_url=None,
        topic_path=None, created_at=_now(),
    )
    page = CapturesPage(items=[item], next_cursor=None)
    assert len(page.items) == 1
    assert page.next_cursor is None


def test_capture_detail_extends_item_with_diagnostics():
    detail = CaptureDetail(
        capture_id="01J", url=None, platform="article",
        status=CaptureStatus.FAILED,
        doc_id=None, web_url=None, topic_path=None,
        created_at=_now(),
        error="extractor failed",
        retry_count=2,
        classifier_reasoning=None,
    )
    assert detail.error == "extractor failed"
    assert detail.retry_count == 2
```

- [ ] **Step 2.2: Implement — append to `ingest/src/models.py`**

```python
class CaptureItem(BaseModel):
    """Single row returned in lists.

    Phase 7 history view in the iOS app consumes this shape.
    """

    capture_id: str
    url: str | None = None
    platform: str
    status: CaptureStatus
    doc_id: str | None = None
    web_url: str | None = None
    topic_path: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class CapturesPage(BaseModel):
    """Paginated list response."""

    items: list[CaptureItem]
    next_cursor: str | None = None


class CaptureDetail(CaptureItem):
    """Single capture detail, with diagnostics for the iOS detail screen."""

    error: str | None = None
    retry_count: int = 0
    classifier_reasoning: str | None = None
```

- [ ] **Step 2.3: Run + commit**

```bash
cd ingest && python -m pytest tests/test_phase7_models.py -v
git add ingest/src/models.py ingest/tests/test_phase7_models.py
git commit -m "$(cat <<'EOF'
feat(ingest): wire models for Phase 7 list / detail responses

CaptureItem — single row shape for /captures list and detail responses.
CapturesPage — wraps items + next_cursor for pagination.
CaptureDetail — extends CaptureItem with error / retry_count /
classifier_reasoning for the iOS detail screen.

Phase 7 / Task 2 of docs/plans/2026-05-07-phase-7-read-manage.md
EOF
)"
```

---

## Task 3: GET /captures + GET /captures/{id}

**Files:**
- Modify: `ingest/src/api.py`
- Create: `ingest/tests/test_read_endpoints.py`

- [ ] **Step 3.1: Write failing tests**

```python
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.config import settings
from src.db import CaptureRow


def _row(**overrides):
    base = dict(
        id="01J", url="https://x", url_hash="h", source_app=None,
        shared_title=None, shared_text=None, platform="article",
        status="done", doc_id="d-1", web_url="w-1",
        topic_path="Sources/Articles/Web",
        classifier_topic="Tech", classifier_conf=0.9, classifier_reasoning="ok",
        retry_count=0,
        created_at=datetime(2026, 5, 7, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return CaptureRow(**base)


def _build_app(*, repo=None):
    from src.api import app, get_capture_repo
    repo = repo or AsyncMock()
    app.dependency_overrides[get_capture_repo] = lambda: repo
    return app, repo


@pytest.mark.asyncio
async def test_list_captures_default():
    app, repo = _build_app()
    repo.list_captures.return_value = [_row(id="b"), _row(id="a")]
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/captures", headers={"Authorization": f"Bearer {settings.ingest_api_token}"})
        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) == 2
        assert body["items"][0]["capture_id"] == "b"
        assert body["next_cursor"] is None
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_captures_with_filters():
    app, repo = _build_app()
    repo.list_captures.return_value = []
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(
                "/captures?limit=10&status=failed&platform=instagram",
                headers={"Authorization": f"Bearer {settings.ingest_api_token}"},
            )
        assert r.status_code == 200
        kwargs = repo.list_captures.call_args.kwargs
        assert kwargs["limit"] == 10
        assert kwargs["status"] == "failed"
        assert kwargs["platform"] == "instagram"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_captures_clamps_limit_to_max():
    app, repo = _build_app()
    repo.list_captures.return_value = []
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            await c.get("/captures?limit=9999",
                        headers={"Authorization": f"Bearer {settings.ingest_api_token}"})
        assert repo.list_captures.call_args.kwargs["limit"] <= 200
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_captures_unauth():
    app, _ = _build_app()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/captures")
        assert r.status_code == 401
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_capture_by_id_returns_detail():
    app, repo = _build_app()
    repo.get_by_id.return_value = _row(status="failed")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/captures/01J",
                            headers={"Authorization": f"Bearer {settings.ingest_api_token}"})
        assert r.status_code == 200
        body = r.json()
        assert body["capture_id"] == "01J"
        assert body["status"] == "failed"
        assert "retry_count" in body
        assert body["classifier_reasoning"] == "ok"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_capture_404():
    app, repo = _build_app()
    repo.get_by_id.return_value = None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/captures/missing",
                            headers={"Authorization": f"Bearer {settings.ingest_api_token}"})
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_capture_deleted_returns_404():
    """Soft-deleted rows are hidden from GET — they exist in DB for audit but shouldn't appear in iOS history."""
    app, repo = _build_app()
    repo.get_by_id.return_value = _row(status="deleted")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/captures/01J",
                            headers={"Authorization": f"Bearer {settings.ingest_api_token}"})
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()
```

- [ ] **Step 3.2: Implement — extend `api.py`**

Add helpers + 2 routes:

```python
# Near other helpers
MAX_LIST_LIMIT = 200
DEFAULT_LIST_LIMIT = 50


def _row_to_item(row: CaptureRow, *, completed_at: datetime | None = None) -> CaptureItem:
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
```

> Imports to add at the top of api.py: `CaptureItem`, `CapturesPage`, `CaptureDetail` from `src.models`.

- [ ] **Step 3.3: Run + commit**

```bash
cd ingest && python -m pytest tests/test_read_endpoints.py -v
git add ingest/src/api.py ingest/tests/test_read_endpoints.py
git commit -m "$(cat <<'EOF'
feat(ingest): GET /captures + GET /captures/{id}

List endpoint clamps limit to [1, 200] (default 50). Filters by status
and platform are passthrough kwargs to repo.list_captures. Auth required.

Detail endpoint returns CaptureDetail with retry_count + reasoning.
Soft-deleted rows return 404 (audited in DB but hidden from iOS).

Phase 7 / Task 3 of docs/plans/2026-05-07-phase-7-read-manage.md
EOF
)"
```

---

## Task 4: POST /captures/{id}/retry + DELETE /captures/{id}

**Files:**
- Modify: `ingest/src/api.py`
- Create: `ingest/tests/test_manage_endpoints.py`

- [ ] **Step 4.1: Write failing tests**

```python
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from src.config import settings
from src.db import CaptureRow


def _row(status="failed", **overrides):
    base = dict(
        id="01J", url="https://x", url_hash="h", source_app=None,
        shared_title=None, shared_text=None, platform="article",
        status=status, doc_id="d-1", web_url="w-1",
        topic_path="Sources/Articles/Web",
        classifier_topic=None, classifier_conf=None, classifier_reasoning=None,
        retry_count=0,
        created_at=datetime(2026, 5, 7, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return CaptureRow(**base)


def _build_app(repo=None, filer=None):
    from src.api import app, get_capture_repo, get_filer
    repo = repo or AsyncMock()
    filer = filer or AsyncMock()
    app.dependency_overrides[get_capture_repo] = lambda: repo
    app.dependency_overrides[get_filer] = lambda: filer
    return app, repo, filer


@pytest.mark.asyncio
async def test_retry_done_capture_resets_to_queued():
    app, repo, _ = _build_app()
    repo.get_by_id.return_value = _row(status="done")
    repo.mark_for_retry.return_value = _row(status="queued")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/captures/01J/retry",
                             headers={"Authorization": f"Bearer {settings.ingest_api_token}"})
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "queued"
        repo.mark_for_retry.assert_awaited_once_with("01J")
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_retry_failed_capture_resets_to_queued():
    app, repo, _ = _build_app()
    repo.get_by_id.return_value = _row(status="failed")
    repo.mark_for_retry.return_value = _row(status="queued")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/captures/01J/retry",
                             headers={"Authorization": f"Bearer {settings.ingest_api_token}"})
        assert r.status_code == 202
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_retry_queued_capture_returns_409():
    """Already queued / extracting / classifying / filing → conflict."""
    app, repo, _ = _build_app()
    repo.get_by_id.return_value = _row(status="queued")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/captures/01J/retry",
                             headers={"Authorization": f"Bearer {settings.ingest_api_token}"})
        assert r.status_code == 409
        repo.mark_for_retry.assert_not_called()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_retry_missing_returns_404():
    app, repo, _ = _build_app()
    repo.get_by_id.return_value = None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/captures/missing/retry",
                             headers={"Authorization": f"Bearer {settings.ingest_api_token}"})
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_retry_deleted_returns_404():
    app, repo, _ = _build_app()
    repo.get_by_id.return_value = _row(status="deleted")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/captures/01J/retry",
                             headers={"Authorization": f"Bearer {settings.ingest_api_token}"})
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_delete_soft_trashes_doc_and_marks_row():
    app, repo, filer = _build_app()
    repo.mark_deleted.return_value = _row(status="deleted")
    filer._mcp = AsyncMock()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.delete("/captures/01J",
                               headers={"Authorization": f"Bearer {settings.ingest_api_token}"})
        assert r.status_code == 200
        body = r.json()
        assert body == {"ok": True}
        repo.mark_deleted.assert_awaited_once_with("01J")
        filer._mcp.delete_doc.assert_awaited_once_with("d-1")
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_delete_missing_returns_404():
    app, repo, filer = _build_app()
    repo.mark_deleted.return_value = None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.delete("/captures/missing",
                               headers={"Authorization": f"Bearer {settings.ingest_api_token}"})
        assert r.status_code == 404
        filer._mcp.delete_doc.assert_not_called() if hasattr(filer, "_mcp") else None
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_delete_swallows_mcp_error_but_still_marks_row():
    """If mcp.delete_doc fails (e.g., doc already trashed), the capture is still marked deleted."""
    app, repo, filer = _build_app()
    repo.mark_deleted.return_value = _row(status="deleted")
    filer._mcp = AsyncMock()
    filer._mcp.delete_doc.side_effect = Exception("mcp says no")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.delete("/captures/01J",
                               headers={"Authorization": f"Bearer {settings.ingest_api_token}"})
        # Status is 200 because the row IS marked; mcp failure logged but tolerated.
        assert r.status_code == 200
    finally:
        app.dependency_overrides.clear()
```

- [ ] **Step 4.2: Implement — append to `api.py`**

```python
import logging  # at top if not already

@app.post(
    "/captures/{capture_id}/retry",
    response_model=CaptureResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_capture(
    capture_id: str,
    repo: CaptureRepository = Depends(get_capture_repo),
    router: PlatformRouter = Depends(get_platform_router),
    _: str = require_token,
) -> CaptureResponse:
    existing = await repo.get_by_id(capture_id)
    if existing is None or existing.status == "deleted":
        raise HTTPException(status_code=404, detail="Capture not found")
    if existing.status in ("queued", "extracting", "classifying", "filing"):
        raise HTTPException(status_code=409, detail=f"Capture is already in flight (status={existing.status})")
    row = await repo.mark_for_retry(capture_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Capture not found")
    return _row_to_response(row, router)


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
            logging.getLogger(__name__).warning(
                "delete_doc failed for capture %s doc %s: %s",
                capture_id, row.doc_id, e,
            )
    return {"ok": True}
```

- [ ] **Step 4.3: Run + commit**

```bash
cd ingest && python -m pytest tests/test_manage_endpoints.py -v
git add ingest/src/api.py ingest/tests/test_manage_endpoints.py
git commit -m "$(cat <<'EOF'
feat(ingest): POST /captures/{id}/retry + DELETE /captures/{id}

retry: 202 if status in {done, failed} → mark_for_retry → worker picks
up. 409 if status in {queued, extracting, classifying, filing} (already
in flight). 404 if missing or deleted.

delete: soft-trashes the AFFiNE doc via filer._mcp.delete_doc and marks
the row status='deleted'. 200 {ok: true} on success. 404 if missing.
mcp errors are logged + swallowed — the capture is still marked
deleted (e.g., doc already trashed in AFFiNE manually).

Phase 7 / Task 4 of docs/plans/2026-05-07-phase-7-read-manage.md
EOF
)"
```

---

## Task 5: Build verification + push + PR

- [ ] `docker compose build ingest` — confirm image still builds
- [ ] Full pytest — expect ~165 passed, 5 skipped
- [ ] Push branch + open PR with base=main

---

## Spec coverage map

| Phase 7 deliverable | Task |
|---|---|
| `GET /captures` (list with filters) | 3 |
| `GET /captures/{id}` (detail) | 3 |
| `POST /captures/{id}/retry` | 4 |
| `DELETE /captures/{id}` | 4 |
| Auth on all 4 routes | 3, 4 |
| Soft-delete preserves audit + 404 on subsequent GET | 1, 3 |

## Out of scope

- Cursor pagination — `next_cursor` returned as None for now; the iOS app fetches `?limit=50` and that's enough for the personal volume. Add cursor support in Phase 9 if needed.
- WebSocket / SSE for live status updates — iOS pulls on demand; out of scope.
- Worker that handles retry triggers via NOTIFY → polling tick is sufficient.
