"""Tests for the YouTube cookies storage helpers + POST endpoint."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from src.youtube_cookies import (
    cookie_file_exists,
    validate_netscape,
    write_cookies_atomic,
)


# ── validate_netscape ──────────────────────────────────────────────


@pytest.mark.parametrize("body, expected_ok", [
    # Valid: header comment + one row
    (
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\tabc123\n",
        True,
    ),
    # Valid: multiple rows + comments
    (
        "# header\n"
        "# more comments\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\tSESSION\tx\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\tNAME2\ty\n",
        True,
    ),
    # Empty: no rows
    ("", False),
    # Only comments: no rows
    ("# only a comment\n# another\n", False),
    # Wrong tab count
    (".youtube.com\tTRUE\t/\tTRUE\t0\tSID\n", False),
    (".youtube.com TRUE / TRUE 0 SID abc\n", False),  # spaces, not tabs
])
def test_validate_netscape_accepts_or_rejects(body, expected_ok):
    ok, _ = validate_netscape(body)
    assert ok is expected_ok


def test_validate_netscape_returns_helpful_error():
    ok, err = validate_netscape("foo\tbar\n")
    assert ok is False
    assert "tabs" in err


# ── write_cookies_atomic ───────────────────────────────────────────


def test_write_cookies_atomic_writes_and_chmods(tmp_path):
    dest = tmp_path / "youtube.txt"
    body = "# header\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n"

    write_cookies_atomic(body, dest)

    assert dest.read_text(encoding="utf-8") == body
    if sys.platform != "win32":
        # chmod is best-effort on Windows; only assert on POSIX.
        mode = stat.S_IMODE(dest.stat().st_mode)
        assert mode == 0o600


def test_write_cookies_atomic_overwrites_existing(tmp_path):
    dest = tmp_path / "youtube.txt"
    write_cookies_atomic("first\n", dest)
    write_cookies_atomic("second\n", dest)
    assert dest.read_text(encoding="utf-8") == "second\n"


def test_write_cookies_atomic_creates_parent_dirs(tmp_path):
    dest = tmp_path / "subdir" / "nested" / "youtube.txt"
    assert not dest.parent.exists()
    write_cookies_atomic("# x\n.y.z\tTRUE\t/\tTRUE\t0\tA\tB\n", dest)
    assert dest.exists()


def test_write_cookies_atomic_no_temp_left_behind(tmp_path):
    """The .tmp sibling should be renamed away — no leftover .tmp file."""
    dest = tmp_path / "youtube.txt"
    write_cookies_atomic("body\n", dest)
    assert not (tmp_path / "youtube.txt.tmp").exists()


# ── cookie_file_exists ─────────────────────────────────────────────


def test_cookie_file_exists_returns_true_for_non_empty(tmp_path):
    p = tmp_path / "y.txt"
    p.write_text("data\n", encoding="utf-8")
    assert cookie_file_exists(p) is True
    assert cookie_file_exists(str(p)) is True


def test_cookie_file_exists_false_for_missing(tmp_path):
    assert cookie_file_exists(tmp_path / "missing.txt") is False


def test_cookie_file_exists_false_for_empty(tmp_path):
    """Empty file = treat as missing — extension hasn't synced yet."""
    p = tmp_path / "empty.txt"
    p.touch()
    assert cookie_file_exists(p) is False


# ── POST /youtube/cookies endpoint ─────────────────────────────────


@pytest.fixture
def app_with_token(tmp_path, monkeypatch):
    """Build a FastAPI test client. Patches settings in-place so the
    existing api module picks up the test token + cookies path without
    needing an importlib.reload (which breaks dependency_overrides
    chained from other tests in the same run)."""
    cookies_dest = tmp_path / "youtube.txt"

    from src.config import settings as live_settings
    monkeypatch.setattr(live_settings, "ingest_api_token", "test-token")
    monkeypatch.setattr(live_settings, "youtube_cookies_path", str(cookies_dest))

    from src.api import app
    from fastapi.testclient import TestClient
    return TestClient(app), cookies_dest


def test_post_youtube_cookies_writes_file(app_with_token):
    client, dest = app_with_token
    body = (
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n"
    )
    resp = client.post(
        "/youtube/cookies",
        content=body,
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "text/plain",
        },
    )
    assert resp.status_code == 204
    assert dest.read_text(encoding="utf-8") == body


def test_post_youtube_cookies_rejects_missing_auth(app_with_token):
    client, _ = app_with_token
    resp = client.post("/youtube/cookies", content="x")
    assert resp.status_code in (401, 403)


def test_post_youtube_cookies_rejects_invalid_format(app_with_token):
    client, dest = app_with_token
    resp = client.post(
        "/youtube/cookies",
        content="this is not netscape format",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 400
    assert not dest.exists() or dest.read_text(encoding="utf-8") == ""


def test_post_youtube_cookies_does_not_log_body(app_with_token, caplog):
    """Regression guard: the cookie body must NEVER appear in logs."""
    import logging
    client, _ = app_with_token
    secret = "SUPER_SECRET_COOKIE_VALUE_42"
    body = (
        f"# header\n"
        f".youtube.com\tTRUE\t/\tTRUE\t0\tSID\t{secret}\n"
    )
    with caplog.at_level(logging.DEBUG):
        client.post(
            "/youtube/cookies",
            content=body,
            headers={"Authorization": "Bearer test-token"},
        )
    # Cookie value must not appear in any log message
    for record in caplog.records:
        assert secret not in record.getMessage()
