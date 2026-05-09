"""yt-dlp video download (Phase 13 first stage).

Replaces the previous cobalt-based path. yt-dlp uses:
  - HTTP_PROXY/HTTPS_PROXY env vars (residential tunnel) — inherited
    by the subprocess automatically.
  - YouTube iOS player_client, which serves format URLs that work without
    poToken. Cookies are deliberately NOT passed (would force yt-dlp to
    skip the iOS client and fall back to web → poToken → bgutil hang).
  - bgutil-ytdlp-pot-provider script mode is wired in the args as a
    fallback for non-iOS clients but should never actually be invoked
    for YouTube on this path.

Returns the path to the downloaded mp4 (`workdir/video.mp4`). Raises
RuntimeError on yt-dlp failure or empty output. Caller (cobalt_ext's
_maybe_run_video_analysis) is responsible for cleanup.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

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
        # Force the iOS player client. The default (web) returns format URLs
        # gated behind poToken, which forces a bgutil call — and bgutil's
        # Node fetch() doesn't honor HTTP_PROXY env vars, so it hangs trying
        # to reach YouTube directly from the cloud IP. The iOS client
        # historically hands back format URLs that work with cookies alone,
        # bypassing the entire poToken minting path. If YouTube tightens the
        # iOS client too (mid-2026 trajectory), bgutil HTTP server mode is
        # the planned next step.
        "--extractor-args",
        "youtube:player_client=ios",
    ]
    # NOTE: --cookies is intentionally NOT passed here. yt-dlp silently
    # SKIPS the iOS player_client whenever cookies are present
    # ("WARNING: [youtube] Skipping client \"ios\" since it does not
    # support cookies"), which forces it back to web → poToken → bgutil
    # script-node → hang on the Innertube fetch (Node fetch ignores
    # HTTP_PROXY). Without cookies, iOS is allowed and serves format URLs
    # that work over the residential proxy without any poToken mint.
    # Trade-off: login-gated content (private/age-restricted/members-only)
    # won't yield Phase 13 keyframes. The captions and audio paths still
    # use cookies (via youtube-transcript-api / cobalt) so transcript
    # content for those videos still lands in the doc — only the inline
    # keyframe images are missing for the auth-gated subset.

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
