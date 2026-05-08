"""Cobalt-based media extractor.

Pipeline:
    1. POST {COBALT_API_URL}/ with the target URL, downloadMode=audio.
    2. Parse the tunnel/redirect URL out of the response.
    3. Stream the audio to a temp file.
    4. Run it through Whisper (reusing ytdlp_ext._whisper_transcribe).
    5. Return Extracted with body_md = the transcript.

Why no rich metadata: cobalt doesn't return reliable title/channel/
description. The iOS client already provides `shared_title`, which the
caller (api.py) uses for the AFFiNE doc title — so leaving title=None
in Extracted doesn't lose anything observable to the user. Author/
published_at are None for the same reason.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import httpx

from src.config import Platform, settings
from src.pipeline.extracted import Extracted, MediaKind, truncate_body
from src.pipeline.extractors import register_extractor
from src.pipeline.extractors.ytdlp_ext import _whisper_transcribe


# Tests inject a MockTransport here. Production leaves it None so httpx
# uses the default networking transport.
_TEST_TRANSPORT: httpx.AsyncBaseTransport | None = None


_COBALT_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)


_TMP_PARENT = "/tmp/ingest"


async def extract(url: str, platform: Platform) -> Extracted:
    # The compose stack mounts /tmp/ingest as a size-capped tmpfs; outside
    # the container (e.g., unit tests) the directory needs creating.
    os.makedirs(_TMP_PARENT, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="ingest-cobalt-", dir=_TMP_PARENT))
    try:
        tunnel_url = await _request_tunnel(url)
        audio_path = await _download_audio(tunnel_url, workdir)
        transcript = await _whisper_transcribe(audio_path)

        body_parts = [f"# {url}", "", "## Transcript (Whisper via cobalt)", "", transcript]
        body_md = truncate_body("\n".join(body_parts), limit=settings.max_body_chars)

        return Extracted(
            title=None,
            body_md=body_md,
            author=None,
            published_at=None,
            media_kind=MediaKind.VIDEO,
            extra={
                "extractor": "cobalt",
                "platform_id": platform.id,
                "tunnel_url": tunnel_url,
            },
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _request_tunnel(url: str) -> str:
    payload = {
        "url": url,
        "downloadMode": "audio",
        "audioFormat": "m4a",
    }
    async with _client() as client:
        try:
            resp = await client.post("/", json=payload)
        except httpx.HTTPError as e:
            raise RuntimeError(f"cobalt http: {type(e).__name__}: {e}") from e

        if resp.status_code >= 400:
            raise RuntimeError(f"cobalt http: status={resp.status_code} body={resp.text[:200]}")

        body = resp.json()

    status = body.get("status")
    if status in ("tunnel", "redirect"):
        tunnel = body.get("url")
        if not tunnel:
            raise RuntimeError(f"cobalt response missing url: {body}")
        return tunnel
    if status == "error":
        err = body.get("error", {}) or {}
        code = err.get("code", "unknown")
        ctx = err.get("context", "")
        raise RuntimeError(f"cobalt error: {code} {ctx}".strip())
    if status == "picker":
        audio_url = body.get("audio")
        if audio_url:
            return audio_url
        raise RuntimeError(f"cobalt picker response had no audio: {body}")

    raise RuntimeError(f"cobalt unexpected status: {status} body={body}")


async def _download_audio(tunnel_url: str, workdir: Path) -> Path:
    out_path = workdir / "audio.m4a"
    async with _client() as client:
        async with client.stream("GET", tunnel_url) as resp:
            if resp.status_code >= 400:
                raise RuntimeError(f"cobalt download: status={resp.status_code}")
            with out_path.open("wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
    return out_path


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


register_extractor("cobalt", extract)
