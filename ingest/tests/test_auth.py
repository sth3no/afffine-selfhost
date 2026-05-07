import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth import require_token
from src.config import settings


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()

    @a.get("/protected")
    def protected(_: str = require_token):  # type: ignore[arg-type]
        return {"ok": True}

    return a


def test_no_authorization_header_returns_401(app: FastAPI):
    client = TestClient(app)
    r = client.get("/protected")
    assert r.status_code == 401
    assert "missing" in r.json()["detail"].lower()


def test_wrong_scheme_returns_401(app: FastAPI):
    client = TestClient(app)
    r = client.get("/protected", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert r.status_code == 401


def test_wrong_token_returns_401(app: FastAPI):
    client = TestClient(app)
    r = client.get("/protected", headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401


def test_correct_token_returns_200(app: FastAPI):
    client = TestClient(app)
    r = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {settings.ingest_api_token}"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
