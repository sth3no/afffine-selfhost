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
