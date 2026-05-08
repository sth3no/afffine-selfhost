"""Cobalt-based media extractor + yt-dlp metadata.

Pipeline:
    1. In parallel: POST {COBALT_API_URL}/ for the audio tunnel AND run
       yt-dlp --skip-download for metadata (title, description, channel).
    2. Stream cobalt audio to a temp file → Whisper transcript.
    3. Compose Extracted: title/author from metadata, body_md =
       description + transcript (in sectioned markdown).

Metadata is best-effort — yt-dlp may fail (e.g. YouTube cookie blocks);
the transcript still goes through. Author / published_at default to None
in that case so the downstream summarizer regenerates the title from
content alone.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

from src.config import Platform, settings
from src.pipeline.extracted import Extracted, MediaKind, truncate_body
from src.pipeline.extractors import register_extractor
from src.pipeline.extractors._ytdlp_metadata import fetch_metadata
from src.pipeline.extractors.ytdlp_ext import _whisper_transcribe

log = logging.getLogger(__name__)


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
        # Fan out metadata fetch + cobalt tunnel request — yt-dlp metadata
        # is independent of cobalt audio download, so run them in parallel
        # to save ~2-5s per capture.
        tunnel_url, metadata = await asyncio.gather(
            _request_tunnel(url),
            fetch_metadata(url),
        )
        audio_path = await _download_audio(tunnel_url, workdir)
        transcript = await _whisper_transcribe(audio_path)

        title, author, description, published_at = _unpack_metadata(metadata)
        body_md = _build_body_md(
            url=url,
            title=title,
            author=author,
            description=description,
            transcript=transcript,
        )

        return Extracted(
            title=title,
            body_md=truncate_body(body_md, limit=settings.max_body_chars),
            author=author,
            published_at=published_at,
            media_kind=MediaKind.VIDEO,
            extra={
                "extractor": "cobalt",
                "platform_id": platform.id,
                "tunnel_url": tunnel_url,
                "url": url,
                "has_metadata": metadata is not None,
                "description": description if description else None,
            },
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _unpack_metadata(
    metadata: dict | None,
) -> tuple[str | None, str | None, str, datetime | None]:
    """Return (title, author, description, published_at) from yt-dlp info.json."""
    if not metadata:
        return None, None, "", None
    title = metadata.get("title") or None
    author = metadata.get("channel") or metadata.get("uploader") or None
    description = (metadata.get("description") or "").strip()
    upload_date_str = metadata.get("upload_date")
    published_at: datetime | None = None
    if upload_date_str:
        try:
            published_at = datetime.strptime(str(upload_date_str), "%Y%m%d").replace(
                tzinfo=timezone.utc,
            )
        except ValueError:
            published_at = None
    return title, author, description, published_at


def _build_body_md(
    *,
    url: str,
    title: str | None,
    author: str | None,
    description: str,
    transcript: str,
) -> str:
    """Compose a sectioned markdown body the orchestrator can split into blocks.

    Section headings drive the orchestrator's block layout — keep these stable.
    """
    parts: list[str] = []
    if title:
        parts.append(f"**{title}**")
    if author:
        parts.append(f"_by {author}_")
    parts.append(f"Source: {url}")
    if description:
        parts.append("## Description")
        parts.append(description)
    parts.append("## Transcript (Whisper via cobalt)")
    parts.append(transcript or "(empty transcript)")
    return "\n\n".join(parts)


async def _request_tunnel(url: str) -> str:
    payload = {
        "url": url,
        "downloadMode": "audio",
        # cobalt v11 accepts only: best | mp3 | ogg | wav | opus.
        # mp3 is the safest cross-platform format for the Whisper API too.
        "audioFormat": "mp3",
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
    # Filename extension matters: OpenAI's Whisper multipart upload sniffs
    # the MIME from the filename. Keep it in sync with audioFormat above.
    out_path = workdir / "audio.mp3"
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
