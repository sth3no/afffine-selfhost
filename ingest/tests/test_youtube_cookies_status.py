"""Tests for the cookie freshness helper + GET /youtube/cookies/status endpoint."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ── cookie_file_status helper ──────────────────────────────────────────


def test_cookie_file_status_missing(tmp_path: Path) -> None:
    """Missing file → exists=False, age None, byte_count 0, mtime None."""
    from src.youtube_cookies import cookie_file_status

    result = cookie_file_status(tmp_path / "nope.txt")
    assert result == {
        "exists": False,
        "age_seconds": None,
        "mtime": None,
        "byte_count": 0,
    }


def test_cookie_file_status_empty_file(tmp_path: Path) -> None:
    """Empty file → treated as missing (matches cookie_file_exists semantics)."""
    from src.youtube_cookies import cookie_file_status

    p = tmp_path / "empty.txt"
    p.touch()
    result = cookie_file_status(p)
    assert result["exists"] is False
    assert result["byte_count"] == 0


def test_cookie_file_status_present(tmp_path: Path) -> None:
    from src.youtube_cookies import cookie_file_status

    p = tmp_path / "cookies.txt"
    body = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tx\n"
    p.write_text(body)
    # Backdate mtime by 60 seconds to assert age > 0.
    past = time.time() - 60
    os.utime(p, (past, past))

    result = cookie_file_status(p)
    assert result["exists"] is True
    assert result["byte_count"] == p.stat().st_size
    assert result["age_seconds"] is not None and result["age_seconds"] >= 60
    # ISO-8601 in UTC, ends with Z (matches what the API returns to the extension).
    assert isinstance(result["mtime"], str) and result["mtime"].endswith("Z")


def test_cookie_file_status_accepts_str_path(tmp_path: Path) -> None:
    from src.youtube_cookies import cookie_file_status

    p = tmp_path / "cookies.txt"
    p.write_text("# h\n.y.z\tTRUE\t/\tTRUE\t0\tA\tB\n")
    result = cookie_file_status(str(p))
    assert result["exists"] is True


# ── GET /youtube/cookies/status endpoint ───────────────────────────────


@pytest.fixture
def app_with_token(tmp_path, monkeypatch):
    """Same in-place settings patch the existing test_youtube_cookies.py uses.

    Avoids importlib.reload — that pattern breaks dependency_overrides
    chained from other tests in the same run.
    """
    cookies_dest = tmp_path / "youtube.txt"

    from src.config import settings as live_settings
    monkeypatch.setattr(live_settings, "ingest_api_token", "test-token")
    monkeypatch.setattr(live_settings, "youtube_cookies_path", str(cookies_dest))

    from src.api import app
    return TestClient(app), cookies_dest


def test_cookies_status_requires_token(app_with_token):
    client, _ = app_with_token
    resp = client.get("/youtube/cookies/status")
    assert resp.status_code in (401, 403)


def test_cookies_status_missing_file(app_with_token):
    client, _ = app_with_token
    resp = client.get(
        "/youtube/cookies/status",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "exists": False,
        "age_seconds": None,
        "mtime": None,
        "byte_count": 0,
    }


def test_cookies_status_present(app_with_token):
    client, dest = app_with_token
    dest.write_text("# header\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tx\n")
    resp = client.get(
        "/youtube/cookies/status",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["exists"] is True
    assert body["byte_count"] > 0
    assert body["age_seconds"] is not None and body["age_seconds"] >= 0
    assert body["mtime"].endswith("Z")


def test_cookies_status_does_not_log_body(app_with_token, caplog):
    """Sanity check that the GET path never logs cookie content."""
    client, dest = app_with_token
    secret = "SUPER_SECRET_COOKIE_VALUE_DO_NOT_LOG"
    dest.write_text(f"# header\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\t{secret}\n")
    with caplog.at_level(logging.DEBUG):
        client.get(
            "/youtube/cookies/status",
            headers={"Authorization": "Bearer test-token"},
        )
    for record in caplog.records:
        assert secret not in record.getMessage()


# ── GET /youtube/cookies/diagnostic ───────────────────────────────────


def test_diagnostic_requires_token(app_with_token):
    client, _ = app_with_token
    resp = client.get("/youtube/cookies/diagnostic")
    assert resp.status_code in (401, 403)


def test_diagnostic_missing_files(app_with_token):
    client, _ = app_with_token
    resp = client.get(
        "/youtube/cookies/diagnostic",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["netscape"]["exists"] is False
    assert body["cobalt"]["exists"] is False
    assert body["netscape"]["cookies"] == []
    assert body["cobalt"]["parse"]["valid_json"] is False


def test_diagnostic_returns_names_not_values(app_with_token):
    """Diagnostic endpoint must NEVER return cookie values."""
    import json

    client, dest = app_with_token
    secret = "EXTREMELY_SECRET_VALUE_42"
    dest.write_text(
        f"# header\n"
        f".youtube.com\tTRUE\t/\tTRUE\t0\tSID\t{secret}\n"
        f".youtube.com\tTRUE\t/\tTRUE\t0\t__Secure-3PSID\tanother_{secret}\n",
    )
    # Also create a sibling cobalt.json in the diagnostic-expected location.
    cobalt_path = dest.parent / "cobalt.json"
    cobalt_path.write_text(
        json.dumps({"youtube": [f"SID={secret}; __Secure-3PSID=another_{secret}"]}),
    )

    resp = client.get(
        "/youtube/cookies/diagnostic",
        headers={"Authorization": "Bearer test-token"},
    )
    body = resp.json()

    # Names appear, values don't.
    netscape_names = {c["name"] for c in body["netscape"]["cookies"]}
    assert netscape_names == {"SID", "__Secure-3PSID"}

    cobalt_names = set(body["cobalt"]["parse"]["services"]["youtube"]["cookie_names"])
    assert cobalt_names == {"SID", "__Secure-3PSID"}

    # Hard regression guard: the secret value must appear NOWHERE in the response.
    raw = resp.text
    assert secret not in raw


def test_diagnostic_flags_invalid_cobalt_json(app_with_token):
    client, dest = app_with_token
    dest.write_text("# header\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tx\n")
    (dest.parent / "cobalt.json").write_text("not valid json {{{")

    resp = client.get(
        "/youtube/cookies/diagnostic",
        headers={"Authorization": "Bearer test-token"},
    )
    body = resp.json()
    assert body["cobalt"]["parse"]["valid_json"] is False
    assert "error" in body["cobalt"]["parse"]
