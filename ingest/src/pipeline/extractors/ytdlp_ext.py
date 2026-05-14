"""yt-dlp + caption parser + Whisper API fallback.

Pipeline:
    1. yt-dlp metadata + auto-subs -> temp dir
    2. If caption present (en or cs preferred): VTT -> plain text -> body_md.
    3. Else if duration <= MAX_TRANSCRIPT_MIN * 60:
       yt-dlp -x audio extract -> Whisper API -> body_md.
    4. Else: body_md = "transcript skipped: video too long".
    5. Always: cleanup temp dir in finally.

Subprocess calls run via asyncio.create_subprocess_exec to avoid blocking
the event loop. The yt-dlp Python lib is also available; we use the CLI
because its caption output is well-defined and easy to parse.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from src.config import Platform, settings
from src.pipeline.extracted import Extracted, MediaKind, truncate_body
from src.pipeline.extractors import register_extractor


async def extract(url: str, platform: Platform, **_kwargs) -> Extracted:
    workdir_path: Path | None = None
    try:
        workdir_path = await _run_ytdlp_metadata(url)
        info = _read_info_json(workdir_path)

        title = info.get("title")
        channel = info.get("channel") or info.get("uploader")
        duration = int(info.get("duration") or 0)
        upload_date = _parse_upload_date(info.get("upload_date"))

        body = await _build_body(url, workdir_path, info, duration)

        return Extracted(
            title=title,
            body_md=truncate_body(body, limit=settings.max_body_chars),
            author=channel,
            published_at=upload_date,
            media_kind=MediaKind.VIDEO,
            extra={
                "extractor": "ytdlp",
                "platform_id": platform.id,
                "duration_seconds": duration,
                "video_id": info.get("id"),
            },
        )
    finally:
        if workdir_path is not None and workdir_path.exists():
            shutil.rmtree(workdir_path, ignore_errors=True)


async def _build_body(url: str, workdir: Path, info: dict, duration: int) -> str:
    """Caption first, Whisper fallback, skip if too long."""
    caption_text = _read_caption_if_present(workdir)
    description = (info.get("description") or "").strip()

    parts = [f"# {info.get('title', '(untitled)')}"]
    if info.get("channel"):
        parts.append(f"_by {info['channel']}_")
    if description:
        parts.append("\n## Description\n\n" + description)

    if caption_text:
        parts.append("\n## Transcript (auto-captions)\n\n" + caption_text)
        return "\n\n".join(parts)

    cap = settings.max_transcript_min * 60
    if duration <= 0 or duration > cap:
        parts.append(f"\n_transcript skipped: duration {duration}s exceeds cap {cap}s._")
        return "\n\n".join(parts)

    # Short video, no caption -- fall back to Whisper.
    audio_path = await _run_ytdlp_audio(url, workdir)
    try:
        transcript, _segments = await _whisper_transcribe(audio_path)
        parts.append("\n## Transcript (Whisper)\n\n" + transcript)
    finally:
        try:
            audio_path.unlink()
        except FileNotFoundError:
            pass
    return "\n\n".join(parts)


def _read_info_json(workdir: Path) -> dict:
    candidates = list(workdir.glob("*.info.json"))
    if not candidates:
        raise FileNotFoundError(f"yt-dlp produced no info.json in {workdir}")
    return json.loads(candidates[0].read_text(encoding="utf-8"))


def _read_caption_if_present(workdir: Path) -> str | None:
    """Return cleaned caption text from any *.vtt in workdir, or None."""
    for ext in ("en", "cs"):
        files = list(workdir.glob(f"*.{ext}.vtt"))
        if files:
            return _vtt_to_text(files[0].read_text(encoding="utf-8"))
    files = list(workdir.glob("*.vtt"))
    if files:
        return _vtt_to_text(files[0].read_text(encoding="utf-8"))
    return None


_VTT_TIMESTAMP = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3} --> \d{2}:\d{2}:\d{2}\.\d{3}.*$")


def _vtt_to_text(vtt: str) -> str:
    """Strip WEBVTT header, cue numbers, timestamps; keep prose lines."""
    lines = []
    for line in vtt.splitlines():
        s = line.strip()
        if not s or s == "WEBVTT" or s.isdigit() or _VTT_TIMESTAMP.match(s):
            continue
        # Strip inline tags like <c.colorE5E5E5> and <00:00:00.480>
        s = re.sub(r"<[^>]+>", "", s)
        if s:
            lines.append(s)
    # Dedupe consecutive duplicates (yt-dlp auto-captions repeat lines).
    out: list[str] = []
    for ln in lines:
        if not out or out[-1] != ln:
            out.append(ln)
    return "\n".join(out)


def _parse_upload_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# -- Subprocess helpers (mocked in unit tests) --------------------------------


async def _run_ytdlp_metadata(url: str) -> Path:
    """Run `yt-dlp --skip-download --write-info-json --write-auto-sub`
    into a fresh temp dir. Returns the dir path."""
    workdir = Path(tempfile.mkdtemp(prefix="ingest-ytdlp-", dir="/tmp/ingest"))
    proc = await asyncio.create_subprocess_exec(
        "yt-dlp",
        "--skip-download",
        "--write-info-json",
        "--write-auto-sub",
        "--sub-lang", "en,cs",
        "--convert-subs", "vtt",
        "-o", str(workdir / "video.%(ext)s"),
        url,
        cwd=str(workdir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp metadata failed: {stderr.decode(errors='replace')}")
    return workdir


async def _run_ytdlp_audio(url: str, workdir: Path) -> Path:
    """Extract audio to m4a in workdir; return the path."""
    proc = await asyncio.create_subprocess_exec(
        "yt-dlp",
        "-x", "--audio-format", "m4a",
        "-o", str(workdir / "audio.%(ext)s"),
        url,
        cwd=str(workdir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp audio failed: {stderr.decode(errors='replace')}")
    files = list(workdir.glob("audio.*"))
    if not files:
        raise FileNotFoundError("yt-dlp produced no audio file")
    return files[0]


async def _whisper_transcribe(audio_path: Path) -> tuple[str, list[dict[str, Any]]]:
    """OpenAI Whisper API. Returns (full_text, segments).

    `segments` is a list of `{"start": float, "end": float, "text": str}`
    dicts derived from Whisper's `verbose_json` response — each ~5-30s of
    speech. Downstream callers (Phase 18 transcript-guided keyframe
    selection) aggregate these into larger ranking windows.

    Empty list if `verbose_json` parsing fails — callers must treat that
    as "no timing info available" and fall back to non-timestamp-aware
    behavior.
    """
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not set; cannot transcribe")
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    with audio_path.open("rb") as f:
        result = await client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="verbose_json",
            timestamp_granularities=["segment"],
        )
    text = (result.text or "").strip()
    segs: list[dict[str, Any]] = []
    raw_segments = getattr(result, "segments", None) or []
    for s in raw_segments:
        # OpenAI SDK returns Pydantic models; older / mocked paths may pass dicts.
        try:
            if hasattr(s, "start"):
                start = float(s.start)
                end = float(s.end)
                seg_text = (s.text or "").strip()
            else:
                start = float(s["start"])
                end = float(s["end"])
                seg_text = (s.get("text") or "").strip()
        except (TypeError, ValueError, KeyError):
            continue
        if seg_text:
            segs.append({"start": start, "end": end, "text": seg_text})
    return text, segs


register_extractor("ytdlp", extract)
