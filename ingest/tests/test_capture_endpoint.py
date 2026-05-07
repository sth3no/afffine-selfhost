import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from src.config import settings


def _build_test_app(*, repo: AsyncMock, filer: AsyncMock, router: MagicMock) -> FastAPI:
    """Construct the FastAPI app with mocked dependencies via overrides."""
    from src.api import app, get_capture_repo, get_filer, get_platform_router

    app.dependency_overrides[get_capture_repo] = lambda: repo
    app.dependency_overrides[get_filer] = lambda: filer
    app.dependency_overrides[get_platform_router] = lambda: router
    return app


@pytest.mark.asyncio
async def test_capture_unauthorized_returns_401():
    repo = AsyncMock()
    filer = AsyncMock()
    router = MagicMock()
    app = _build_test_app(repo=repo, filer=filer, router=router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/capture", json={"url": "https://example.com"})
        assert r.status_code == 401
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_capture_happy_path_creates_stub_and_returns_202():
    repo = AsyncMock()
    repo.get_by_url_hash.return_value = None  # not yet captured

    filer = AsyncMock()
    filer.resolve_or_create_folder.return_value = "f-instagram"

    # mock the inner mcp.create_doc by stubbing filer's mcp attribute
    mcp = AsyncMock()
    mcp.create_doc.return_value = {"docId": "doc-abc-123"}
    filer._mcp = mcp

    router = MagicMock()
    plat = MagicMock(id="instagram", group="Socials", folder_name="Instagram", extractor="ytdlp")
    router.detect.return_value = plat
    router.initial_path.return_value = ["Sources", "Socials", "Instagram"]

    app = _build_test_app(repo=repo, filer=filer, router=router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/capture",
                json={
                    "url": "https://www.instagram.com/p/Cxyz/",
                    "shared_title": "Honey-glazed salmon",
                    "source_app": "Instagram",
                },
                headers={"Authorization": f"Bearer {settings.ingest_api_token}"},
            )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["doc_id"] == "doc-abc-123"
        assert body["status"] == "queued"
        assert body["platform"] == "instagram"
        assert body["initial_path"] == "Sources/Socials/Instagram"
        assert body["capture_id"]
        assert "doc-abc-123" in body["web_url"]

        repo.insert.assert_called_once()
        inserted_row = repo.insert.call_args.args[0]
        assert inserted_row.platform == "instagram"
        assert inserted_row.doc_id == "doc-abc-123"
        assert inserted_row.status == "queued"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_capture_idempotent_returns_existing_without_writing():
    from src.db import CaptureRow
    from datetime import datetime, timezone

    repo = AsyncMock()
    existing = CaptureRow(
        id="01J-existing",
        url="https://www.instagram.com/p/Cxyz/",
        url_hash="hash",
        source_app=None,
        shared_title="prior",
        shared_text=None,
        platform="instagram",
        status="done",
        doc_id="prev-doc",
        web_url="https://affine.example.com/.../prev-doc",
        topic_path="Sources/Socials/Instagram",
        created_at=datetime(2026, 5, 6, tzinfo=timezone.utc),
    )
    repo.get_by_url_hash.return_value = existing

    filer = AsyncMock()
    router = MagicMock()
    router.detect.return_value = MagicMock(id="instagram", group="Socials", folder_name="Instagram")
    router.initial_path.return_value = ["Sources", "Socials", "Instagram"]

    app = _build_test_app(repo=repo, filer=filer, router=router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/capture",
                json={"url": "https://www.instagram.com/p/Cxyz/?utm_source=test"},
                headers={"Authorization": f"Bearer {settings.ingest_api_token}"},
            )
        assert r.status_code == 202
        body = r.json()
        assert body["capture_id"] == "01J-existing"
        assert body["doc_id"] == "prev-doc"
        # NOTHING was written or filed.
        repo.insert.assert_not_called()
        filer.resolve_or_create_folder.assert_not_called()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_capture_rejects_missing_url_and_text():
    """Spec: at least one of url/shared_text required. Handler enforces."""
    repo = AsyncMock()
    filer = AsyncMock()
    router = MagicMock()
    app = _build_test_app(repo=repo, filer=filer, router=router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/capture",
                json={},
                headers={"Authorization": f"Bearer {settings.ingest_api_token}"},
            )
        assert r.status_code == 400
        assert "url or shared_text" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_capture_with_extra_field_returns_422():
    repo = AsyncMock()
    filer = AsyncMock()
    router = MagicMock()
    app = _build_test_app(repo=repo, filer=filer, router=router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/capture",
                json={"url": "https://example.com", "unexpected": "field"},
                headers={"Authorization": f"Bearer {settings.ingest_api_token}"},
            )
        assert r.status_code == 422
    finally:
        app.dependency_overrides.clear()
