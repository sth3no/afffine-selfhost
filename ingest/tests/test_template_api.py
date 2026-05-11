"""Tests for /templates/* endpoints (Phase 14)."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api import app, app_state
from src.pipeline.templates import ContentTemplate


def _tmpl(**ov) -> ContentTemplate:
    base = dict(
        id="t1", platform_id="youtube", topic="Tutorials",
        name="YouTube Tutorial v1", system_prompt="prompt",
        status="edited", generator_meta=None, created_by="user",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    base.update(ov)
    return ContentTemplate(**base)


@asynccontextmanager
async def _noop_lifespan(app):
    """No-op lifespan so TestClient doesn't need a real DB / MCP / affine token."""
    yield


@pytest.fixture
def client(monkeypatch):
    """Inject a mocked TemplatesRepository into app_state."""
    from src.config import settings

    repo = AsyncMock()
    monkeypatch.setattr(app_state, "templates_repo", repo, raising=False)
    # Patch the settings object directly (it is created at import time, so
    # monkeypatch.setenv alone is too late).
    monkeypatch.setattr(settings, "ingest_api_token", "test-token")
    # Swap the lifespan with a no-op so TestClient doesn't fail on missing
    # AFFINE_ACCESS_TOKEN / DATABASE_URL during startup.
    monkeypatch.setattr(app.router, "lifespan_context", _noop_lifespan)
    with TestClient(app) as c:
        yield c, repo


HEADERS = {"Authorization": "Bearer test-token"}


def test_get_templates_returns_list(client):
    c, repo = client
    repo.list_all = AsyncMock(return_value=[_tmpl(), _tmpl(id="t2", platform_id="*", topic="*")])
    repo.count_usage = AsyncMock(return_value=5)

    r = c.get("/templates", headers=HEADERS)

    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert body[0]["usage_count"] == 5


def test_get_templates_with_filters(client):
    c, repo = client
    repo.list_all = AsyncMock(return_value=[])
    repo.count_usage = AsyncMock(return_value=0)

    r = c.get("/templates?platform=youtube&status_filter=edited", headers=HEADERS)

    assert r.status_code == 200
    repo.list_all.assert_awaited_once()
    kwargs = repo.list_all.await_args.kwargs
    assert kwargs["platform_id"] == "youtube"
    assert kwargs["status"] == "edited"


def test_get_template_by_id(client):
    c, repo = client
    repo.get = AsyncMock(return_value=_tmpl())
    repo.count_usage = AsyncMock(return_value=3)

    r = c.get("/templates/t1", headers=HEADERS)

    assert r.status_code == 200
    assert r.json()["id"] == "t1"
    assert r.json()["usage_count"] == 3


def test_get_template_404(client):
    c, repo = client
    repo.get = AsyncMock(return_value=None)
    r = c.get("/templates/nope", headers=HEADERS)
    assert r.status_code == 404


def test_post_template_creates(client):
    c, repo = client
    repo.create = AsyncMock(return_value=_tmpl())
    repo.count_usage = AsyncMock(return_value=0)

    r = c.post("/templates", headers=HEADERS, json={
        "platform_id": "youtube", "topic": "Tutorials",
        "name": "YouTube Tutorial v1", "system_prompt": "prompt",
    })

    assert r.status_code == 201
    assert r.json()["id"] == "t1"


def test_post_template_409_on_conflict(client):
    c, repo = client
    import asyncpg
    repo.create = AsyncMock(side_effect=asyncpg.UniqueViolationError("dup"))

    r = c.post("/templates", headers=HEADERS, json={
        "platform_id": "youtube", "topic": "Tutorials",
        "name": "x", "system_prompt": "x",
    })

    assert r.status_code == 409


def test_put_template_updates(client):
    c, repo = client
    repo.update = AsyncMock(return_value=_tmpl(name="new name"))
    repo.count_usage = AsyncMock(return_value=0)

    r = c.put("/templates/t1", headers=HEADERS, json={"name": "new name"})

    assert r.status_code == 200
    assert r.json()["name"] == "new name"


def test_put_template_404(client):
    c, repo = client
    repo.update = AsyncMock(return_value=None)
    r = c.put("/templates/nope", headers=HEADERS, json={"name": "x"})
    assert r.status_code == 404


def test_delete_template_archives(client):
    c, repo = client
    archived = _tmpl(status="archived")
    repo.get = AsyncMock(return_value=_tmpl())
    repo.archive = AsyncMock(return_value=archived)
    repo.list_all = AsyncMock(return_value=[])  # used for seed-protection check
    repo.count_usage = AsyncMock(return_value=0)

    r = c.delete("/templates/t1", headers=HEADERS)

    assert r.status_code == 200
    assert r.json()["status"] == "archived"


def test_delete_seed_default_rejected_when_last_one(client):
    c, repo = client
    seed = _tmpl(id="seed", platform_id="*", topic="*", status="auto")
    repo.get = AsyncMock(return_value=seed)
    repo.list_all = AsyncMock(return_value=[seed])  # ONLY (*, *) row → reject

    r = c.delete("/templates/seed", headers=HEADERS)

    assert r.status_code == 409
    assert "seed" in r.text.lower() or "default" in r.text.lower()


def test_resolve_returns_template(client):
    c, repo = client
    repo.resolve = AsyncMock(return_value=_tmpl())
    repo.count_usage = AsyncMock(return_value=0)

    r = c.get("/templates/resolve?platform=youtube&topic=Tutorials", headers=HEADERS)

    assert r.status_code == 200
    assert r.json()["id"] == "t1"


def test_resolve_404_when_no_match(client):
    c, repo = client
    repo.resolve = AsyncMock(return_value=None)
    r = c.get("/templates/resolve?platform=x&topic=y", headers=HEADERS)
    assert r.status_code == 404


def test_synthesize_creates_new_template(client, monkeypatch):
    c, repo = client
    repo.resolve = AsyncMock(return_value=None)
    repo.count_usage = AsyncMock(return_value=0)

    new_tmpl = _tmpl(id="t_new", platform_id="youtube", topic="Recipes", status="auto")
    fake_synth = AsyncMock(return_value=new_tmpl)
    monkeypatch.setattr("src.api.synthesize_template", fake_synth, raising=False)

    # Mock the captures lookup by patching the pool's fetchrow.
    fake_row = {"extracted_snapshot": {
        "title": "T", "body_md": "B",
        "author": None, "media_kind": "video", "extra": {},
    }}
    fake_pool = MagicMock()
    fake_conn = AsyncMock()
    fake_conn.fetchrow = AsyncMock(return_value=fake_row)
    # async context manager for pool.acquire():
    fake_pool.acquire = MagicMock()
    fake_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(app_state, "pool", fake_pool, raising=False)

    r = c.post("/templates/synthesize", headers=HEADERS, json={
        "platform_id": "youtube", "topic": "Recipes",
    })

    assert r.status_code == 201
    assert r.json()["id"] == "t_new"


def test_put_template_422_on_empty_body(client):
    c, repo = client
    r = c.put("/templates/t1", headers=HEADERS, json={})
    assert r.status_code == 422


def test_synthesize_409_when_active_template_exists(client):
    c, repo = client
    repo.resolve = AsyncMock(return_value=_tmpl())

    r = c.post("/templates/synthesize", headers=HEADERS, json={
        "platform_id": "youtube", "topic": "Tutorials",
    })

    assert r.status_code == 409
