"""Cobalt video download (vs. audio-only).

Mirrors `cobalt_ext._download_audio` but asks cobalt for the merged
video+audio stream at 720p (sweet spot — high enough for keyframe
analysis, low enough to keep tmpfs usage predictable). Used by Phase 13
video_analysis.py to feed PySceneDetect + Claude vision.

Returns the path to the downloaded mp4. Raises RuntimeError on cobalt
failure or when the file would exceed `cobalt_video_max_size_mb`.
Caller is responsible for cleanup (typically via the same workdir as
audio download).
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from src.config import settings

log = logging.getLogger(__name__)


# Tests inject a MockTransport. Production leaves None (default httpx).
_TEST_TRANSPORT: httpx.AsyncBaseTransport | None = None

_COBALT_TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=5.0)


async def download_video(url: str, workdir: Path) -> Path:
    """Download merged video+audio mp4 to `workdir/video.mp4`.

    Mirrors `cobalt_ext._download_audio`'s empty-body guard: cobalt's
    video tunnel can return HTTP 200 with an empty / HTML body when its
    upstream YT fetch silently failed (typically: no poToken, stale
    cookies, or YT serving a format URL that 403s). A 0-byte file
    crashes PySceneDetect's OpenCV-based reader with a useless message
    ("Failed to open video"); we raise a descriptive RuntimeError
    pointing at the real cause instead.
    """
    tunnel_url = await _request_video_tunnel(url)
    out_path = workdir / "video.mp4"
    max_bytes = settings.cobalt_video_max_size_mb * 1024 * 1024
    total = 0
    async with _client() as client:
        async with client.stream("GET", tunnel_url) as resp:
            if resp.status_code >= 400:
                raise RuntimeError(f"cobalt video download: status={resp.status_code}")
            with out_path.open("wb") as f:
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        f.close()
                        out_path.unlink(missing_ok=True)
                        raise RuntimeError(
                            f"cobalt video download exceeded "
                            f"{settings.cobalt_video_max_size_mb} MB cap",
                        )
                    f.write(chunk)

    # Empty-body guard. A real video at 720p is at minimum a few hundred
    # KB even for a 10-second clip; 64 KB is well below any plausible
    # video and well above any HTML error body.
    _MIN_VIDEO_BYTES = 64 * 1024
    if total < _MIN_VIDEO_BYTES:
        out_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"cobalt video too small: {total} bytes — cobalt's tunnel "
            f"returned an empty / error body. Most common cause: cobalt "
            f"is missing a working poToken from yt_session_server (check "
            f"`docker logs affine_cobalt | grep -i potoken` for "
            f"`[✓] loaded poToken` vs `[!] Failed loading poToken`). "
            f"Phase 13 keyframes will be empty until cobalt's video fetch "
            f"actually succeeds."
        )

    log.info(
        "cobalt video downloaded",
        extra={"byte_count": total, "path": str(out_path)},
    )
    return out_path


async def _request_video_tunnel(url: str) -> str:
    """POST to cobalt for a video tunnel URL. 720p is the sweet spot."""
    payload = {
        "url": url,
        "downloadMode": "auto",
        # cobalt v11 video qualities: 144 / 240 / 360 / 480 / 720 / 1080 / max.
        # 720 reads text on screens cleanly and stays under the size cap.
        "videoQuality": "720",
    }
    async with _client() as client:
        try:
            resp = await client.post("/", json=payload)
        except httpx.HTTPError as e:
            raise RuntimeError(f"cobalt video http: {type(e).__name__}: {e}") from e
        if resp.status_code >= 400:
            raise RuntimeError(
                f"cobalt video http: status={resp.status_code} body={resp.text[:200]}",
            )
        body = resp.json()

    status = body.get("status")
    if status in ("tunnel", "redirect"):
        tunnel = body.get("url")
        if not tunnel:
            raise RuntimeError(f"cobalt video response missing url: {body}")
        return tunnel
    if status == "error":
        err = body.get("error", {}) or {}
        raise RuntimeError(f"cobalt video error: {err.get('code', 'unknown')}")
    if status == "picker":
        # Picker → cobalt couldn't pick one variant; bail (rare for normal videos)
        raise RuntimeError(f"cobalt video returned picker (multiple variants): {body}")
    raise RuntimeError(f"cobalt video unexpected status: {status}")


def _client() -> httpx.AsyncClient:
    kwargs: dict = {
        "base_url": settings.cobalt_api_url.rstrip("/"),
        "timeout": _COBALT_TIMEOUT,
        "headers": {
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    }
    if _TEST_TRANSPORT is not None:
        kwargs["transport"] = _TEST_TRANSPORT
    return httpx.AsyncClient(**kwargs)
