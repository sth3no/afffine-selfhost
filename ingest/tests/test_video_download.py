"""Tests for yt-dlp video download (Phase 13 first stage).

Refactored from cobalt+httpx mocking to yt-dlp subprocess mocking after
the cobalt video tunnel proved unreliable (BotGuard-via-headless-Chromium
timed out reliably through the residential proxy; see
docs/plans/2026-05-09-yt-dlp-video-bypass.md).
"""

from __future__ import annotations

from pathlib import Path

import pytest


class _FakeProc:
    """Minimal stand-in for asyncio.subprocess.Process."""

    def __init__(self, returncode: int, stderr: bytes = b""):
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self):
        return b"", self._stderr

    def kill(self):  # called on timeout in the implementation
        pass


def _make_fake_exec(*, returncode: int, stderr: bytes = b"",
                    write_video_bytes: int | None = None):
    """Returns a coroutine compatible with asyncio.create_subprocess_exec.

    When `write_video_bytes` is set, writes that many bytes to whatever
    output path yt-dlp was invoked with (the value of the `-o` arg).
    Mirrors what real yt-dlp does on success.
    """
    async def _fake_exec(*args, **_kwargs):
        if write_video_bytes is not None:
            argv = list(args)
            try:
                o_idx = argv.index("-o")
                out_path = Path(argv[o_idx + 1])
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(b"\x00" * write_video_bytes)
            except (ValueError, IndexError):
                pass
        return _FakeProc(returncode=returncode, stderr=stderr)
    return _fake_exec


@pytest.mark.asyncio
async def test_download_video_happy_path(monkeypatch, tmp_path: Path):
    """yt-dlp returncode 0 + a real file at the requested path → returns the path."""
    import asyncio
    from src.pipeline.extractors import _video_download as vd

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_exec(returncode=0, write_video_bytes=128 * 1024),
    )

    out_path = await vd.download_video(
        "https://www.youtube.com/watch?v=abc",
        tmp_path,
    )
    assert out_path.exists()
    assert out_path.name == "video.mp4"
    assert out_path.stat().st_size == 128 * 1024


@pytest.mark.asyncio
async def test_download_video_subprocess_failure_raises(monkeypatch, tmp_path: Path):
    """Non-zero yt-dlp returncode → RuntimeError with stderr context."""
    import asyncio
    from src.pipeline.extractors import _video_download as vd

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_exec(
            returncode=1,
            stderr=b"ERROR: [youtube] abc: Sign in to confirm you're not a bot",
        ),
    )

    with pytest.raises(RuntimeError, match=r"yt-dlp video.*Sign in"):
        await vd.download_video(
            "https://www.youtube.com/watch?v=abc",
            tmp_path,
        )


@pytest.mark.asyncio
async def test_download_video_empty_file_raises_descriptive_error(
    monkeypatch, tmp_path: Path,
):
    """yt-dlp succeeded (rc=0) but produced a tiny / empty file. We treat
    this as failure with a poToken-pointing message — same defensive guard
    the previous cobalt impl had."""
    import asyncio
    from src.pipeline.extractors import _video_download as vd

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_exec(returncode=0, write_video_bytes=512),  # < 64 KB
    )

    with pytest.raises(RuntimeError, match=r"yt-dlp video too small.*poToken"):
        await vd.download_video(
            "https://www.youtube.com/watch?v=abc",
            tmp_path,
        )
    assert not (tmp_path / "video.mp4").exists()


@pytest.mark.asyncio
async def test_download_video_no_output_file_raises(monkeypatch, tmp_path: Path):
    """yt-dlp returned 0 but didn't write the expected file (corner case
    when yt-dlp's --output template doesn't match what we asked for, or
    permissions failure). Caller must see a clear error, not None."""
    import asyncio
    from src.pipeline.extractors import _video_download as vd

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_exec(returncode=0, write_video_bytes=None),
    )

    with pytest.raises(RuntimeError, match=r"yt-dlp video.*no output file"):
        await vd.download_video(
            "https://www.youtube.com/watch?v=abc",
            tmp_path,
        )


@pytest.mark.asyncio
async def test_download_video_does_not_pass_cookies(monkeypatch, tmp_path: Path):
    """yt-dlp silently SKIPS the iOS player_client whenever --cookies is
    passed ("Skipping client 'ios' since it does not support cookies"),
    which forces the web client → poToken → bgutil hang. So we explicitly
    do NOT pass --cookies on the video download path even when a cookies
    file exists. Cookies stay on the captions / metadata paths via
    youtube-transcript-api / cobalt."""
    import asyncio
    from src.pipeline.extractors import _video_download as vd
    from src.config import settings

    cookies_path = tmp_path / "youtube.txt"
    cookies_path.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setattr(settings, "youtube_cookies_path", str(cookies_path))

    captured: dict = {}

    async def _capturing_exec(*args, **_kwargs):
        captured["args"] = list(args)
        argv = list(args)
        o_idx = argv.index("-o")
        out_path = Path(argv[o_idx + 1])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"\x00" * (128 * 1024))
        return _FakeProc(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _capturing_exec)

    await vd.download_video("https://www.youtube.com/watch?v=abc", tmp_path)

    argv = captured["args"]
    assert "--cookies" not in argv, (
        f"expected --cookies absent (would force web client + bgutil hang) but "
        f"found in: {argv}"
    )


@pytest.mark.asyncio
async def test_download_video_passes_potoken_extractor_args(monkeypatch, tmp_path: Path):
    """The yt-dlp invocation must wire bgutil-ytdlp-pot-provider via
    --extractor-args pointing at /opt/bgutil-pot/server. Without that,
    yt-dlp won't find the script-mode provider and video format URLs 403."""
    import asyncio
    from src.pipeline.extractors import _video_download as vd

    captured: dict = {}

    async def _capturing_exec(*args, **_kwargs):
        captured["args"] = list(args)
        argv = list(args)
        o_idx = argv.index("-o")
        out_path = Path(argv[o_idx + 1])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"\x00" * (128 * 1024))
        return _FakeProc(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _capturing_exec)

    await vd.download_video("https://www.youtube.com/watch?v=abc", tmp_path)

    argv = captured["args"]
    assert any(
        "youtubepot-bgutilscript:server_home=/opt/bgutil-pot/server" in a
        for a in argv
    ), f"missing bgutil extractor-args in: {argv}"


@pytest.mark.asyncio
async def test_download_video_forces_ios_player_client(monkeypatch, tmp_path: Path):
    """The yt-dlp invocation must force YouTube's iOS player client to
    bypass the web client's poToken requirement (which would otherwise
    trigger bgutil's hanging Innertube fetch)."""
    import asyncio
    from src.pipeline.extractors import _video_download as vd

    captured: dict = {}

    async def _capturing_exec(*args, **_kwargs):
        captured["args"] = list(args)
        argv = list(args)
        o_idx = argv.index("-o")
        out_path = Path(argv[o_idx + 1])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"\x00" * (128 * 1024))
        return _FakeProc(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _capturing_exec)

    await vd.download_video("https://www.youtube.com/watch?v=abc", tmp_path)

    argv = captured["args"]
    assert any(
        "youtube:player_client=ios" in a for a in argv
    ), f"missing iOS player_client extractor-args in: {argv}"
