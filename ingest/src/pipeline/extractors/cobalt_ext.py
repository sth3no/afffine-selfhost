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
from src.pipeline.extractors._youtube_oembed import fetch_youtube_oembed
from src.pipeline.extractors._youtube_transcript import fetch_youtube_transcript
from src.pipeline.extractors._ytdlp_metadata import fetch_metadata
from src.pipeline.extractors.ytdlp_ext import _whisper_transcribe

log = logging.getLogger(__name__)


# Cobalt's well-known YouTube bot-block code. Returned as
# `{"status":"error","error":{"code":"error.api.youtube.login"}}` on a 400.
_YOUTUBE_BOT_BLOCK_FRAGMENTS = (
    "error.api.youtube.login",
    "youtube.api.youtube.login",
    "sign in to confirm",
)


# Tests inject a MockTransport here. Production leaves it None so httpx
# uses the default networking transport.
_TEST_TRANSPORT: httpx.AsyncBaseTransport | None = None


_COBALT_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)


_TMP_PARENT = "/tmp/ingest"


async def extract(
    url: str,
    platform: Platform,
    *,
    mcp_client: object | None = None,
    capture_id: str | None = None,
    **_kwargs,
) -> Extracted:
    """Extract audio + (optional Phase 13) video keyframes for a URL.

    `mcp_client` and `capture_id` are kwargs passed by the orchestrator
    when the call is in the production worker; legacy callers / tests
    that don't pass them get audio-only behavior. `**_kwargs` swallows
    forward-compat additions.
    """
    # The compose stack mounts /tmp/ingest as a size-capped tmpfs; outside
    # the container (e.g., unit tests) the directory needs creating.
    os.makedirs(_TMP_PARENT, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="ingest-cobalt-", dir=_TMP_PARENT))
    try:
        try:
            # Fan out metadata fetch + cobalt tunnel request — yt-dlp metadata
            # is independent of cobalt audio download, so run them in parallel
            # to save ~2-5s per capture.
            tunnel_url, metadata = await asyncio.gather(
                _request_tunnel(url),
                fetch_metadata(url),
            )
        except RuntimeError as e:
            # YouTube has been escalating bot detection in 2026 — both
            # cobalt and yt-dlp need cookies to scrape video pages. Rather
            # than hard-fail the capture (and force manual retries), fall
            # back to YouTube's unauthenticated oEmbed endpoint to at
            # least recover the title + uploader. The summarizer + filer
            # can then run on metadata alone, so the doc gets a real
            # title and lands in a topic folder instead of stuck "failed".
            if platform.id == "youtube" and _is_youtube_bot_block(e):
                log.warning(
                    "youtube bot-block hit; falling back to oEmbed-only metadata: %s", e,
                )
                return await _youtube_metadata_only(url, platform)
            raise

        audio_path = await _download_audio(tunnel_url, workdir)
        transcript = await _whisper_transcribe(audio_path)

        # Phase 13: optional video frame analysis. Best-effort — failures
        # leave the audio-only path untouched.
        video_summary, keyframes = await _maybe_run_video_analysis(
            url=url,
            workdir=workdir,
            transcript=transcript,
            mcp_client=mcp_client,
            capture_id=capture_id,
        )

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
                # Phase 13 fields — None when video_analysis is disabled or fails.
                "video_summary": video_summary,
                "keyframes": [
                    {
                        "blob_source_id": k.blob_source_id,
                        "caption": k.caption,
                        "timestamp_seconds": k.timestamp_seconds,
                    }
                    for k in keyframes
                ],
                "video_analysis_ok": video_summary is not None or len(keyframes) > 0,
            },
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _maybe_run_video_analysis(
    *,
    url: str,
    workdir: Path,
    transcript: str,
    mcp_client: object | None,
    capture_id: str | None,
):
    """Best-effort video download + scene-detect + vision call.

    Returns (video_summary, keyframes). Both empty on disable / failure.
    """
    if not settings.video_analysis_enabled:
        return None, []
    if mcp_client is None or capture_id is None:
        # No way to upload blobs — skip silently. (Tests that just want
        # audio path don't need this.)
        return None, []

    try:
        from src.pipeline.extractors._video_download import download_video
        from src.pipeline.video_analysis import analyze_video

        video_path = await download_video(url, workdir)
        summary, keyframes = await analyze_video(
            video_path=video_path,
            workdir=workdir,
            transcript=transcript,
            capture_id=capture_id,
            mcp_client=mcp_client,
        )
        log.info(
            "video_analysis: ok summary=%s keyframes=%d",
            summary is not None, len(keyframes),
        )
        return summary, keyframes
    except Exception as e:  # noqa: BLE001 — by design
        log.warning("video_analysis skipped: %s", e)
        return None, []


def _is_youtube_bot_block(exc: BaseException) -> bool:
    """True when the error is YouTube refusing without cookies."""
    msg = str(exc).lower()
    return any(frag in msg for frag in _YOUTUBE_BOT_BLOCK_FRAGMENTS)


async def _youtube_metadata_only(url: str, platform: Platform) -> Extracted:
    """YouTube fallback path: oEmbed metadata + transcript-api captions.

    Runs both fetches in parallel:
      - oEmbed:        unauthenticated, gets title + author
      - transcript-api: scrapes YT's caption URL, no auth needed for any
                       video that has captions enabled (auto or manual)

    Either or both can fail. If we get neither, the Extracted still
    returns with title=None and a "transcript unavailable" body — the
    summarizer can still produce a fallback title from the URL host.

    Sets `extra.transcript_source` to one of:
      "youtube_captions"  — got real captions
      "unavailable"       — neither metadata nor captions worked
    """
    oembed, captions = await asyncio.gather(
        fetch_youtube_oembed(url),
        fetch_youtube_transcript(url),
    )

    title = oembed.get("title") if oembed else None
    author = oembed.get("author_name") if oembed else None

    if captions:
        # We have a real transcript — produce the same body shape as the
        # cobalt happy path so the orchestrator's markdown→blocks parser
        # emits Summary / Description / Transcript headings consistently.
        body_md = _build_body_md(
            url=url,
            title=title,
            author=author,
            description="",
            transcript=captions,
            transcript_heading="## Transcript (YouTube captions)",
        )
        transcript_source = "youtube_captions"
    else:
        body_md = _build_body_md_metadata_only(url=url, title=title, author=author)
        transcript_source = "unavailable"

    return Extracted(
        title=title,
        body_md=truncate_body(body_md, limit=settings.max_body_chars),
        author=author,
        published_at=None,
        media_kind=MediaKind.VIDEO,
        extra={
            "extractor": "youtube_oembed_fallback",
            "platform_id": platform.id,
            "url": url,
            "transcript_source": transcript_source,
            "transcript_unavailable": captions is None,
            "has_metadata": oembed is not None,
        },
    )


def _build_body_md_metadata_only(*, url: str, title: str | None, author: str | None) -> str:
    """Body for the no-transcript fallback. Mirrors _build_body_md's section
    layout so the orchestrator's markdown→blocks parser produces the same
    Summary / Description / Transcript heading structure.
    """
    parts: list[str] = []
    if title:
        parts.append(f"**{title}**")
    if author:
        parts.append(f"_by {author}_")
    parts.append(f"Source: {url}")
    parts.append("## Transcript")
    parts.append(
        "_Unavailable — YouTube blocked the audio download AND no captions "
        "were available for this video. Watch the original to see the content._",
    )
    return "\n\n".join(parts)


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
    transcript_heading: str = "## Transcript (Whisper via cobalt)",
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
    parts.append(transcript_heading)
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
    byte_count = 0
    async with _client() as client:
        async with client.stream("GET", tunnel_url) as resp:
            if resp.status_code >= 400:
                raise RuntimeError(f"cobalt download: status={resp.status_code}")
            with out_path.open("wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
                    byte_count += len(chunk)

    # Cobalt's tunnel can return HTTP 200 with an empty / HTML body when
    # the upstream YT fetch silently failed. Whisper then chokes with a
    # cryptic "Invalid file format / duration 0" — detect here so the
    # error message points at the real cause (and the orchestrator can
    # gracefully fall back to oEmbed-only metadata).
    # 4 KB is well below any real audio file (a 5-second mp3 is ~80 KB)
    # and well above any plausible HTML error body (~300 bytes).
    _MIN_AUDIO_BYTES = 4096
    if byte_count < _MIN_AUDIO_BYTES:
        raise RuntimeError(
            f"cobalt audio too small: {byte_count} bytes — cobalt likely "
            f"failed silently on the upstream YT fetch (audio body was "
            f"empty or an error page). Expect oEmbed-only fallback."
        )

    log.info(
        "cobalt audio downloaded",
        extra={"byte_count": byte_count, "path": str(out_path)},
    )
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
