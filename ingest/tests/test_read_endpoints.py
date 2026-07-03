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
async def test_list_captures_full_page_returns_next_cursor():
    """A full page means there may be older rows — next_cursor is the last
    item's created_at, ready to pass back as ?cursor=."""
    app, repo = _build_app()
    repo.list_captures.return_value = [
        _row(id="b", created_at=datetime(2026, 7, 2, tzinfo=timezone.utc)),
        _row(id="a", created_at=datetime(2026, 7, 1, tzinfo=timezone.utc)),
    ]
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/captures", params={"limit": 2},
                            headers={"Authorization": f"Bearer {settings.ingest_api_token}"})
        assert r.status_code == 200
        assert r.json()["next_cursor"] == "2026-07-01T00:00:00+00:00"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_captures_cursor_forwards_before_to_repo():
    app, repo = _build_app()
    repo.list_captures.return_value = []
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/captures",
                            params={"cursor": "2026-07-01T00:00:00+00:00"},
                            headers={"Authorization": f"Bearer {settings.ingest_api_token}"})
        assert r.status_code == 200
        kwargs = repo.list_captures.call_args.kwargs
        assert kwargs["before"] == datetime(2026, 7, 1, tzinfo=timezone.utc)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_captures_invalid_cursor_400():
    app, repo = _build_app()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/captures", params={"cursor": "not-a-timestamp"},
                            headers={"Authorization": f"Bearer {settings.ingest_api_token}"})
        assert r.status_code == 400
        repo.list_captures.assert_not_called()
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
async def test_get_capture_detail_exposes_error_and_completed_at():
    """The iOS detail screen needs to show WHY a capture failed (and when a
    successful one finished) without the operator grepping server logs."""
    app, repo = _build_app()
    completed = datetime(2026, 5, 7, 12, 30, tzinfo=timezone.utc)
    repo.get_by_id.return_value = _row(
        status="failed",
        error="cobalt error: error.api.youtube.login",
        completed_at=completed,
        retry_count=3,
    )
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/captures/01J",
                            headers={"Authorization": f"Bearer {settings.ingest_api_token}"})
        assert r.status_code == 200
        body = r.json()
        assert body["error"] == "cobalt error: error.api.youtube.login"
        assert body["completed_at"] is not None
        assert body["retry_count"] == 3
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
