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


# Cobalt failure modes that should trigger the YouTube oEmbed-only
# fallback path (instead of failing the capture and retrying forever).
#
# Two kinds of failure end up here:
#   - Explicit bot-block: cobalt returns
#     `{"status":"error","error":{"code":"error.api.youtube.login"}}` on
#     a 400. Cobalt knows YouTube wanted auth.
#   - Silent upstream failure: cobalt's tunnel returns HTTP 200 with a
#     0-byte body (or HTML error page) because YouTube refused server-
#     side and cobalt didn't propagate the error. _download_audio's
#     min-bytes guard turns this into "cobalt audio too small: N bytes".
#     Same recovery path as the explicit case — captions API + oEmbed.
#
# Match is substring-based against `str(exc).lower()`.
_YOUTUBE_RECOVERABLE_FAILURE_FRAGMENTS = (
    # Explicit bot-block codes
    "error.api.youtube.login",
    "youtube.api.youtube.login",
    "sign in to confirm",
    # Silent upstream failure (empty audio/video body from cobalt tunnel)
    "cobalt audio too small",
    "cobalt video too small",
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
        # Phase 12.5 fix #9: captions-first transcript strategy for YouTube.
        # YT auto-captions via youtube-transcript-api are FREE, FAST, and
        # match what users see in the YT UI. They cover ~80% of public
        # videos. Cobalt audio + Whisper is slower and costs ~$0.006/min;
        # only run that path when captions don't exist.
        # Non-YouTube platforms (Instagram, X, TikTok) skip this branch
        # and go straight to the cobalt path — no caption equivalent.
        captions_transcript: str | None = None
        if platform.id == "youtube":
            try:
                captions_transcript = await fetch_youtube_transcript(url)
            except Exception as e:  # noqa: BLE001 — best-effort, fall through to audio path
                log.warning("captions probe failed: %s", e)
                captions_transcript = None

        # Acquire transcript + metadata. The cobalt audio path is wrapped
        # in the SAME try/except as the tunnel request so that 0-byte
        # responses from `_download_audio` (cobalt's silent upstream
        # failure mode) get the same YT recovery treatment as the
        # explicit `error.api.youtube.login` block.
        # Phase 18: when available, raw Whisper / YT-caption segments
        # accompany the transcript so the transcript-guided keyframe
        # selection can rank speech windows by visual-anchor likelihood.
        # Empty list on either branch is a valid "no timing info" sentinel
        # — the downstream ranking step falls back to non-timestamp-aware
        # candidate picking when segments are missing.
        whisper_segments: list[dict] = []
        try:
            if captions_transcript:
                # Captions worked — skip the cobalt audio download AND the
                # Whisper API call. We still fetch metadata in parallel so
                # title/author/description are populated; tunnel_url is
                # unused on this path.
                metadata = await fetch_metadata(url)
                tunnel_url = ""
                transcript = captions_transcript
                transcript_heading = "## Transcript (YouTube captions)"
                log.info("transcript via youtube-transcript-api (captions, free)",
                         extra={"char_count": len(transcript)})
            else:
                # No captions — fall back to cobalt audio + Whisper.
                tunnel_url, metadata = await asyncio.gather(
                    _request_tunnel(url),
                    fetch_metadata(url),
                )
                audio_path = await _download_audio(tunnel_url, workdir)
                transcript, whisper_segments = await _whisper_transcribe(audio_path)
                transcript_heading = "## Transcript (Whisper via cobalt)"
                log.info("transcript via cobalt + Whisper (no captions available)",
                         extra={"char_count": len(transcript)})
        except RuntimeError as e:
            # YouTube has been escalating bot detection in 2026 — both
            # cobalt and yt-dlp need cookies to scrape video pages. Rather
            # than hard-fail the capture (and force manual retries), fall
            # back to YouTube's unauthenticated oEmbed endpoint to at
            # least recover the title + uploader.
            #
            # Two cobalt failure modes route here: explicit bot-block
            # ("error.api.youtube.login") AND silent upstream failure
            # ("cobalt audio too small: 0 bytes" — cobalt's tunnel
            # returned 200 with an empty body because YouTube refused
            # server-side). Both share the same recovery path.
            if platform.id == "youtube" and _is_youtube_recoverable_failure(e):
                log.warning(
                    "youtube cobalt failure; falling back to oEmbed-only metadata: %s", e,
                )
                return await _youtube_metadata_only(url, platform)
            raise

        # Phase 13: optional video frame analysis. Best-effort — failures
        # leave the audio-only path untouched. Independent of which
        # transcript path was used (downloads video separately).
        # Phase 18: whisper_segments (when present) carries Whisper's
        # per-segment timestamps; for the YT-captions path the segments
        # are parsed from the markdown transcript inside analyze_video.
        video_summary, keyframes = await _maybe_run_video_analysis(
            url=url,
            workdir=workdir,
            transcript=transcript,
            whisper_segments=whisper_segments,
            mcp_client=mcp_client,
            capture_id=capture_id,
        )

        title, author, description, published_at = _unpack_metadata(metadata)

        # When yt-dlp metadata fails (bot-walled or video-formats issue),
        # fall through to oEmbed for title + author. Unauthenticated and
        # cheap, and it's what the metadata-only fallback already uses.
        # Especially important on the captions-first path where we never
        # hit the bot-block fallback that would have called oEmbed.
        if title is None and platform.id == "youtube":
            try:
                oembed = await fetch_youtube_oembed(url)
                if oembed:
                    title = oembed.get("title") or title
                    author = oembed.get("author_name") or author
            except Exception as e:  # noqa: BLE001
                log.warning("oEmbed metadata fallback failed: %s", e)
        body_md = _build_body_md(
            url=url,
            title=title,
            author=author,
            description=description,
            transcript=transcript,
            transcript_heading=transcript_heading,
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
                "transcript_source": "youtube_captions" if captions_transcript else "whisper",
                "transcript_unavailable": False,  # both paths produced a transcript
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
    whisper_segments: list[dict] | None = None,
    mcp_client: object | None,
    capture_id: str | None,
):
    """Best-effort video download + scene-detect + vision call.

    Returns (video_summary, keyframes). Both empty on disable / failure.

    This function is heavily logged at each stage so when keyframes don't
    appear in the doc, the operator can grep for `video_analysis:` and
    immediately see WHICH stage failed (download / scenedetect / vision /
    blob upload).
    """
    if not settings.video_analysis_enabled:
        log.info("video_analysis: skipped — VIDEO_ANALYSIS_ENABLED=false")
        return None, []
    if mcp_client is None or capture_id is None:
        log.info(
            "video_analysis: skipped — caller missing mcp_client/capture_id "
            "(legacy test path or shared_text capture)",
        )
        return None, []

    log.info("video_analysis: starting", extra={"url": url})

    try:
        from src.pipeline.extractors._video_download import download_video
        from src.pipeline.video_analysis import analyze_video

        try:
            video_path = await download_video(url, workdir)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "video_analysis: video DOWNLOAD failed (cobalt video tunnel) — %s: %s",
                type(e).__name__, e,
            )
            return None, []

        try:
            video_size = video_path.stat().st_size
        except OSError:
            video_size = -1
        log.info(
            "video_analysis: video downloaded",
            extra={"path": str(video_path), "byte_count": video_size},
        )

        summary, keyframes = await analyze_video(
            video_path=video_path,
            workdir=workdir,
            transcript=transcript,
            whisper_segments=whisper_segments,
            capture_id=capture_id,
            mcp_client=mcp_client,
        )
        log.info(
            "video_analysis: complete",
            extra={
                "summary_chars": len(summary) if summary else 0,
                "keyframe_count": len(keyframes),
                "summary_present": summary is not None,
            },
        )
        if summary is None and not keyframes:
            log.warning(
                "video_analysis: produced NEITHER summary nor keyframes — "
                "check vision call + blob upload logs above",
            )
        return summary, keyframes
    except Exception as e:  # noqa: BLE001 — by design
        log.warning("video_analysis: unexpected top-level error: %s: %s",
                    type(e).__name__, e)
        return None, []


def _is_youtube_recoverable_failure(exc: BaseException) -> bool:
    """True when a cobalt failure is recoverable via the oEmbed-only path.

    Covers both explicit YouTube bot-blocks and silent upstream failures
    (cobalt 0-byte tunnel responses) — see _YOUTUBE_RECOVERABLE_FAILURE_FRAGMENTS.
    """
    msg = str(exc).lower()
    return any(frag in msg for frag in _YOUTUBE_RECOVERABLE_FAILURE_FRAGMENTS)


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

    Description is intentionally NOT included here even though we receive it
    as an arg — the orchestrator emits it separately from `extra["description"]`
    via its own `## Description` block. Including it both places caused the
    description to render twice (once via body_md → _markdown_to_blocks, once
    via extra). The arg is kept in the signature for back-compat with callers
    and tests that still pass it.
    """
    del description  # rendered by orchestrator from extra["description"] instead
    parts: list[str] = []
    if title:
        parts.append(f"**{title}**")
    if author:
        parts.append(f"_by {author}_")
    parts.append(f"Source: {url}")
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
