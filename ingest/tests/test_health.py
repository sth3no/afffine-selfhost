import pytest
from httpx import ASGITransport, AsyncClient

from src.api import app


@pytest.mark.asyncio
async def test_health_returns_ok_envelope():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["queue_depth"] == 0
    assert body["worker_alive"] is False
    assert isinstance(body["version"], str) and len(body["version"]) > 0


@pytest.mark.asyncio
async def test_diagnostic_logging_endpoint_reports_topology():
    """Operators curl this when prod logs look mangled; payload tells
    them whether the issue is in the code (extra handlers) or in the
    display layer (Portainer rendering JSON differently)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/diagnostic/logging")

    assert response.status_code == 200
    body = response.json()
    # Required keys must always be present so the operator's curl never
    # 500s on missing fields.
    assert "json_formatter_active" in body
    assert "root_handler_count" in body
    assert "root_formatter" in body
    assert "extra_handler_loggers" in body
    assert isinstance(body["extra_handler_loggers"], list)
