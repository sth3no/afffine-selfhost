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
