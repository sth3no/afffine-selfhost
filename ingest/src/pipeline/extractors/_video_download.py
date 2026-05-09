"""yt-dlp video download (Phase 13 first stage).

Replaces the previous cobalt-based path. yt-dlp uses:
  - Cookies from settings.youtube_cookies_path (when present).
  - HTTP_PROXY/HTTPS_PROXY env vars (residential tunnel) — inherited
    by the subprocess automatically.
  - bgutil-ytdlp-pot-provider script mode for poToken generation. The
    plugin's BgUtils-based JS server lives at /opt/bgutil-pot/server
    (set up in the Dockerfile). Pure-Node — no Chromium, no proxy
    fragility, unlike the previous yt_session_server sidecar.

Returns the path to the downloaded mp4 (`workdir/video.mp4`). Raises
RuntimeError on yt-dlp failure or empty output. Caller (cobalt_ext's
_maybe_run_video_analysis) is responsible for cleanup.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from src.config import settings
from src.youtube_cookies import cookie_file_exists

log = logging.getLogger(__name__)


_DOWNLOAD_TIMEOUT_SECONDS = 300.0
_MIN_VIDEO_BYTES = 64 * 1024
_BGUTIL_SERVER_HOME = "/opt/bgutil-pot/server"


async def download_video(url: str, workdir: Path) -> Path:
    """Download a 720p mp4 to `workdir/video.mp4` via yt-dlp.

    yt-dlp picks the best video+audio under 720p and merges them. The
    cap matches what we used for the cobalt path — high enough for
    keyframe analysis, low enough to keep tmpfs usage predictable.
    """
    out_path = workdir / "video.mp4"
    workdir.mkdir(parents=True, exist_ok=True)

    ytdlp_args: list[str] = [
        "yt-dlp",
        # -v emits "[debug] Loaded plugin: ..." + "[youtube] Looking up POT
        # provider: ..." which is exactly what we need to see when downloads
        # fail. Stderr is only surfaced to the caller on rc!=0, so happy-path
        # logs aren't bloated by it.
        "-v",
        "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "--merge-output-format", "mp4",
        "--extractor-args",
        f"youtubepot-bgutilscript:server_home={_BGUTIL_SERVER_HOME}",
    ]
    if cookie_file_exists(settings.youtube_cookies_path):
        ytdlp_args += ["--cookies", settings.youtube_cookies_path]

    ytdlp_args += ["-o", str(out_path), url]

    proc = await asyncio.create_subprocess_exec(
        *ytdlp_args,
        cwd=str(workdir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )
    try:
        _, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=_DOWNLOAD_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        proc.kill()
        # Drain whatever stderr was buffered before the kill — that's the
        # only window into what yt-dlp/bgutil were doing during the hang.
        # With -v on the invocation, this will surface the [bgutil*] /
        # [youtube] debug lines preceding the freeze.
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=2.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            stderr = b""
        full = stderr.decode(errors="replace")
        tail = full[-2000:].strip() if len(full) > 2000 else full.strip()
        raise RuntimeError(
            f"yt-dlp video timed out after {_DOWNLOAD_TIMEOUT_SECONDS}s. "
            f"stderr tail (last 2000 chars):\n{tail or '(empty)'}"
        ) from None

    if proc.returncode != 0:
        # Take the LAST 2000 chars of stderr — the actual error and any
        # "[bgutil*]" / "[youtube]" debug lines immediately preceding it
        # are at the END of -v output, not the start.
        full = stderr.decode(errors="replace")
        msg = full[-2000:].strip() if len(full) > 2000 else full.strip()
        raise RuntimeError(f"yt-dlp video failed (rc={proc.returncode}): {msg}")

    if not out_path.exists():
        raise RuntimeError(
            "yt-dlp video succeeded (rc=0) but no output file at "
            f"{out_path} — check yt-dlp output template / permissions"
        )

    size = out_path.stat().st_size
    if size < _MIN_VIDEO_BYTES:
        out_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"yt-dlp video too small: {size} bytes — most likely the "
            f"poToken provider failed (check bgutil-pot logs) or the "
            f"video has restrictions cookies don't unlock. Phase 13 "
            f"keyframes will be empty."
        )

    log.info(
        "yt-dlp video downloaded",
        extra={"byte_count": size, "path": str(out_path)},
    )
    return out_path
