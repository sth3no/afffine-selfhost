"""Tests for cobalt video download (Phase 13's first stage)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest


@pytest.mark.asyncio
async def test_download_video_happy_path(monkeypatch, tmp_path: Path):
    """Successful download produces an mp4 with the streamed bytes."""
    from src.pipeline.extractors import _video_download as vd

    # 128 KB stub — well above the 64 KB MIN_VIDEO_BYTES threshold.
    fake_video = b"\x00" * (128 * 1024)

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/" and request.method == "POST":
            return httpx.Response(
                200,
                json={"status": "tunnel", "url": "http://cobalt:9000/tunnel/abc"},
            )
        if request.url.path.startswith("/tunnel/"):
            return httpx.Response(200, content=fake_video)
        return httpx.Response(404)

    monkeypatch.setattr(vd, "_TEST_TRANSPORT", httpx.MockTransport(_handler), raising=False)

    out_path = await vd.download_video("https://www.youtube.com/watch?v=abc", tmp_path)
    assert out_path.exists()
    assert out_path.read_bytes() == fake_video
    assert out_path.name == "video.mp4"


@pytest.mark.asyncio
async def test_download_video_empty_body_raises_descriptive_error(monkeypatch, tmp_path: Path):
    """Phase 12.5 fix #11: cobalt's video tunnel can return HTTP 200 with
    an empty body when its upstream YT fetch silently failed (typically:
    no poToken, stale cookies). Without the guard, we save a 0-byte file
    and PySceneDetect crashes with a useless OpenCV error. With the guard,
    we raise a descriptive RuntimeError pointing at poToken as the most
    likely cause."""
    from src.pipeline.extractors import _video_download as vd

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"status": "tunnel", "url": "http://cobalt:9000/tunnel/empty"},
            )
        # Tunnel GET returns 200 OK but empty body — exact scenario seen
        # in production when cobalt has cookies but no poToken.
        return httpx.Response(200, content=b"")

    monkeypatch.setattr(vd, "_TEST_TRANSPORT", httpx.MockTransport(_handler), raising=False)

    with pytest.raises(RuntimeError, match="cobalt video too small.*poToken"):
        await vd.download_video("https://www.youtube.com/watch?v=abc", tmp_path)

    # No leftover 0-byte file
    assert not (tmp_path / "video.mp4").exists()


@pytest.mark.asyncio
async def test_download_video_size_cap_enforced(monkeypatch, tmp_path: Path):
    """Settings.cobalt_video_max_size_mb caps the download. Streams beyond
    that abort + delete the partial file."""
    from src.pipeline.extractors import _video_download as vd
    from src.config import settings

    monkeypatch.setattr(settings, "cobalt_video_max_size_mb", 1)

    # 2 MB — exceeds the 1 MB cap.
    big_video = b"\x00" * (2 * 1024 * 1024)

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"status": "tunnel", "url": "http://cobalt:9000/tunnel/big"},
            )
        return httpx.Response(200, content=big_video)

    monkeypatch.setattr(vd, "_TEST_TRANSPORT", httpx.MockTransport(_handler), raising=False)

    with pytest.raises(RuntimeError, match="exceeded.*MB cap"):
        await vd.download_video("https://www.youtube.com/watch?v=abc", tmp_path)

    assert not (tmp_path / "video.mp4").exists()
