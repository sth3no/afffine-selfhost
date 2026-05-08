"""Shared yt-dlp metadata fetcher.

Best-effort: returns the parsed info.json dict, or None on any failure.
Used by extractors that fetch audio/video via a different transport (e.g.
cobalt) but still want yt-dlp's metadata (title, description, uploader).

Phase 12: when a YouTube cookies file is present at
`settings.youtube_cookies_path` (uploaded by the browser extension), we
pass `--cookies` to yt-dlp so authenticated YT requests bypass the bot
detection. The flag is omitted when the file is missing — yt-dlp errors
on a missing cookies path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

from src.config import settings
from src.youtube_cookies import cookie_file_exists

log = logging.getLogger(__name__)

_TMP_PARENT = "/tmp/ingest"
_METADATA_TIMEOUT_SECONDS = 30.0


async def fetch_metadata(url: str) -> dict | None:
    """Run `yt-dlp --skip-download` and return the parsed info.json.

    Returns None on any failure — caller proceeds without metadata.
    """
    os.makedirs(_TMP_PARENT, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="ingest-meta-", dir=_TMP_PARENT))
    try:
        ytdlp_args = [
            "yt-dlp",
            "--skip-download",
            "--write-info-json",
            "--no-warnings",
            "--quiet",
        ]
        # Inject auth cookies when available — bypasses YT's "sign in to
        # confirm you're not a bot" page on cloud IPs.
        if cookie_file_exists(settings.youtube_cookies_path):
            ytdlp_args += ["--cookies", settings.youtube_cookies_path]

        ytdlp_args += [
            "-o", str(workdir / "video.%(ext)s"),
            url,
        ]
        proc = await asyncio.create_subprocess_exec(
            *ytdlp_args,
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_METADATA_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            log.warning("yt-dlp metadata fetch timed out for %s", url)
            return None

        if proc.returncode != 0:
            log.warning("yt-dlp metadata fetch failed (rc=%s): %s",
                        proc.returncode, stderr.decode(errors="replace")[:300])
            return None

        candidates = list(workdir.glob("*.info.json"))
        if not candidates:
            log.warning("yt-dlp metadata fetch: no info.json produced for %s", url)
            return None

        return json.loads(candidates[0].read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 — best-effort by design
        log.warning("yt-dlp metadata fetch errored for %s: %s", url, e)
        return None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
