"""Video frame analysis — scene detection + Claude vision call + blob upload.

Pipeline (per video, all stages best-effort):

    1. PySceneDetect ContentDetector → scene boundaries
    2. ffmpeg single-frame extract at each boundary → JPEG files in workdir
    3. Pillow resize each frame to longest-edge = settings.frame_long_edge_px
    4. Claude Sonnet 4.6 multimodal call:
       input  = transcript + N frame images
       output = grounded summary + per-frame caption + importance 0-10
    5. Filter to top-N by importance (>= settings.keyframe_importance_threshold)
    6. Upload kept frames to AFFiNE blob storage via mcp-ext upload_blob
    7. Return KeyframeRef[] with sourceIds + captions

The orchestrator emits image blocks for these in the doc body.

Failures degrade gracefully — caller (cobalt_ext) catches and continues
with the audio-only path.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.config import settings
from src.llm_clients import anthropic_client
from src.llm_usage import record_anthropic_usage
from src.pipeline.video_analysis_filters import filter_low_quality_frames

log = logging.getLogger(__name__)


@dataclass
class KeyframeRef:
    """A keyframe that's been uploaded to AFFiNE and is ready to embed."""

    blob_source_id: str
    caption: str
    timestamp_seconds: float


@dataclass
class _ExtractedFrame:
    """Internal: a frame on disk with its source timestamp."""

    path: Path
    timestamp_seconds: float
    index: int  # 0-based, by timestamp asc


class _FrameCaption(BaseModel):
    """Claude's per-frame response shape."""

    frame_index: int = Field(description="Index of the frame as numbered in the input.")
    caption: str = Field(description="One sentence describing what's visually in the frame.")
    importance: int = Field(
        description=(
            "0-10. 0 = transition / black / blurry / no useful content. "
            "10 = essential to understanding (UI screens, code, recipe steps, charts)."
        ),
        ge=0, le=10,
    )


class _VisionAnalysis(BaseModel):
    """Claude's full structured response for a video."""

    summary: str = Field(
        description=(
            "Markdown bulleted list (3-6 items) of the most exciting / surprising "
            "/ actionable things in the video. Each bullet on its own line, starts "
            "with '- ', one short punchy line each. Ground in BOTH the audio "
            "transcript (if any) AND what the keyframes show. Default language is "
            "ENGLISH — translate from any other language. Exception: if the "
            "source content is Czech or Slovak, keep bullets in the source language."
        ),
    )
    keyframes: list[_FrameCaption] = Field(
        description="One entry per frame_index sent. Order by frame_index ascending.",
    )


_VISION_SYSTEM_PROMPT = """You analyze short videos for a personal knowledge base.

You will be given:
  - An optional audio transcript (may be empty for music videos / blocked YT)
  - A series of N keyframes extracted from the video, numbered starting at 0

Output strict JSON matching the SummaryResult schema:
  - `summary`: a markdown BULLETED LIST (3-6 items) of the most exciting,
    surprising, or actionable things from the content. Each bullet on its
    own line, starts with "- ", one short punchy line each. NO intro
    sentence, NO outro, NO sub-bullets — just the flat list. Ground in BOTH
    the transcript AND what the frames show. If transcript is empty, rely
    on visual content alone. If they conflict, prioritize what's visible.
    NEVER invent details that appear in neither.
  - `keyframes`: list of {frame_index, caption, importance} — one per
    input frame, in frame_index order. Importance 0-10 (see schema docs).

Caption rules:
  - One sentence, present tense, plain language.
  - Describe what's actually visible: text on screen, UI elements, faces,
    actions, colors that matter. Don't speculate beyond the frame.

Language rules:
  - Default output language is ENGLISH — translate the summary and captions
    to English regardless of the source-content language.
  - EXCEPTION: if the source content is Czech or Slovak, keep the summary
    bullets and captions in the original Czech/Slovak. Don't translate
    those.
  - This applies to both the transcript text and any visible on-screen
    text in the keyframes.
"""


# ── Public entry point ────────────────────────────────────────────


async def analyze_video(
    *,
    video_path: Path,
    workdir: Path,
    transcript: str,
    capture_id: str,
    mcp_client: Any,
) -> tuple[str | None, list[KeyframeRef]]:
    """Run the full pipeline. Returns (grounded_summary, keyframes).

    `grounded_summary` is None when the vision call fails — the orchestrator
    falls back to the text-only summarizer in that case. `keyframes` is
    empty when nothing reaches the importance threshold.
    """
    # Stage 1+2: scene detect + frame extract (sync libs, run in thread).
    try:
        frames = await asyncio.to_thread(
            _detect_and_extract_frames,
            video_path,
            workdir,
            settings.max_frames_per_video,
        )
    except Exception as e:  # noqa: BLE001 — best-effort
        log.warning("video_analysis: scene detection failed: %s", e)
        return None, []

    if not frames:
        log.info("video_analysis: no scenes detected; skipping vision call")
        return None, []

    # Stage 2.5 — Phase 16 quality pre-filter (cheap heuristics; runs sync in a thread).
    # Drops obviously-useless frames (black/uniform/duplicate) before paying the
    # vision-call token cost. Pure-Python (numpy + imagehash) so safe to run
    # in a thread pool. Failure should never block the pipeline.
    try:
        frames = await asyncio.to_thread(filter_low_quality_frames, frames)
    except Exception as e:  # noqa: BLE001
        log.warning("video_analysis: quality filter failed (continuing with unfiltered frames): %s", e)

    if not frames:
        log.info("video_analysis: quality filter dropped all frames; skipping vision call")
        return None, []

    # Stage 3: resize via Pillow (still sync; fold into the to_thread).
    try:
        await asyncio.to_thread(_resize_frames_in_place, frames, settings.frame_long_edge_px)
    except Exception as e:  # noqa: BLE001
        log.warning("video_analysis: frame resize failed: %s", e)
        # Continue — original-resolution frames are still usable, just costlier.

    # Stage 4: Claude Sonnet 4.6 vision call.
    try:
        analysis = await _vision_call(transcript=transcript, frames=frames)
    except Exception as e:  # noqa: BLE001
        log.warning("video_analysis: vision call failed: %s", e)
        return None, []

    # Stage 5: keep top-N by importance, in chronological order.
    by_idx = {fc.frame_index: fc for fc in analysis.keyframes}
    kept_frames: list[tuple[_ExtractedFrame, _FrameCaption]] = []
    for f in frames:
        fc = by_idx.get(f.index)
        if fc is None:
            continue
        if fc.importance < settings.keyframe_importance_threshold:
            continue
        kept_frames.append((f, fc))
    # Cap at max_keyframes_in_doc
    kept_frames = kept_frames[: settings.max_keyframes_in_doc]
    log.info(
        "video_analysis: kept %d keyframes out of %d (threshold=%d)",
        len(kept_frames), len(frames), settings.keyframe_importance_threshold,
    )

    # Stage 6: upload kept frames to AFFiNE blob storage.
    keyframe_refs: list[KeyframeRef] = []
    for frame, caption in kept_frames:
        try:
            data = frame.path.read_bytes()
            blob_filename = f"{capture_id}-frame-{frame.index:02d}.jpg"
            result = await mcp_client.upload_blob(
                filename=blob_filename,
                content_type="image/jpeg",
                data=data,
            )
            source_id = result.get("sourceId") if isinstance(result, dict) else None
            if not source_id:
                log.warning("video_analysis: upload_blob returned no sourceId for %s", blob_filename)
                continue
            keyframe_refs.append(KeyframeRef(
                blob_source_id=str(source_id),
                caption=caption.caption,
                timestamp_seconds=frame.timestamp_seconds,
            ))
        except Exception as e:  # noqa: BLE001
            log.warning("video_analysis: blob upload failed for frame %d: %s", frame.index, e)

    return analysis.summary, keyframe_refs


# ── Stage 1+2: scene detect + ffmpeg extract ──────────────────────


def _detect_and_extract_frames(
    video_path: Path,
    workdir: Path,
    max_frames: int,
) -> list[_ExtractedFrame]:
    """Synchronous: PySceneDetect → ffmpeg seek+extract per scene."""
    import subprocess

    try:
        from scenedetect import detect, ContentDetector
    except ImportError as e:
        log.warning("scenedetect not installed: %s", e)
        return []

    scene_list = detect(
        str(video_path),
        ContentDetector(threshold=settings.scenedetect_threshold),
    )
    if not scene_list:
        # No scene cuts detected. Fall back to fixed-interval samples
        # (25%, 50%, 75% of video duration) for short / single-shot videos.
        return _fallback_fixed_interval_frames(video_path, workdir, n=3)

    # Take the first frame of each scene — cap at max_frames evenly spread.
    if len(scene_list) > max_frames:
        step = len(scene_list) / max_frames
        picked_indices = [int(i * step) for i in range(max_frames)]
        picks = [scene_list[i] for i in picked_indices]
    else:
        picks = scene_list

    out: list[_ExtractedFrame] = []
    for idx, (start, _end) in enumerate(picks):
        ts = start.get_seconds()
        frame_path = workdir / f"frame-{idx:02d}.jpg"
        if not _ffmpeg_extract_frame(video_path, ts, frame_path):
            continue
        out.append(_ExtractedFrame(path=frame_path, timestamp_seconds=ts, index=idx))
    return out


def _fallback_fixed_interval_frames(
    video_path: Path,
    workdir: Path,
    n: int = 3,
) -> list[_ExtractedFrame]:
    """When scene detection finds nothing (single-shot videos), sample at
    fixed 25/50/75% positions of duration."""
    import subprocess

    try:
        # ffprobe to get duration in seconds
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, check=False, timeout=10,
        )
        duration = float(proc.stdout.strip()) if proc.returncode == 0 else 0.0
    except (ValueError, OSError):
        duration = 0.0

    if duration <= 0:
        return []

    out: list[_ExtractedFrame] = []
    for i in range(n):
        ts = duration * (i + 1) / (n + 1)  # 25%, 50%, 75% for n=3
        frame_path = workdir / f"frame-{i:02d}.jpg"
        if _ffmpeg_extract_frame(video_path, ts, frame_path):
            out.append(_ExtractedFrame(path=frame_path, timestamp_seconds=ts, index=i))
    return out


def _ffmpeg_extract_frame(video_path: Path, timestamp_seconds: float, out_path: Path) -> bool:
    """Single-frame extract via ffmpeg. Returns True on success."""
    import subprocess

    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                # -ss BEFORE -i is much faster (seek demuxer) but slightly less
                # precise. For keyframe captures that's fine.
                "-ss", f"{timestamp_seconds:.3f}",
                "-i", str(video_path),
                "-frames:v", "1",
                "-q:v", "2",  # high JPEG quality, ~80% smaller than PNG
                "-y",         # overwrite
                str(out_path),
            ],
            capture_output=True, check=False, timeout=15,
        )
        return proc.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("ffmpeg extract failed at t=%.2fs: %s", timestamp_seconds, e)
        return False


# ── Stage 3: Pillow resize ────────────────────────────────────────


def _resize_frames_in_place(frames: list[_ExtractedFrame], long_edge_px: int) -> None:
    """Resize each frame to longest-edge = long_edge_px (preserve aspect)."""
    try:
        from PIL import Image
    except ImportError as e:
        log.warning("Pillow not installed: %s", e)
        return

    for frame in frames:
        try:
            with Image.open(frame.path) as im:
                im.load()
                w, h = im.size
                long_edge = max(w, h)
                if long_edge <= long_edge_px:
                    continue
                scale = long_edge_px / long_edge
                new_size = (int(w * scale), int(h * scale))
                resized = im.resize(new_size, Image.Resampling.LANCZOS)
                resized.save(frame.path, "JPEG", quality=85, optimize=True)
        except Exception as e:  # noqa: BLE001
            log.warning("frame resize skipped (frame %d): %s", frame.index, e)


# ── Stage 4: Claude Sonnet 4.6 multimodal vision call ─────────────


async def _vision_call(
    *,
    transcript: str,
    frames: list[_ExtractedFrame],
) -> _VisionAnalysis:
    """Single multimodal call with N frames + transcript → structured analysis.

    Uses messages.parse(output_format=_VisionAnalysis) so Sonnet enforces the
    schema server-side. Frames are sent inline as base64 image blocks.
    """
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY missing — cannot run vision call")

    client = anthropic_client()

    # Build the multimodal user content.
    transcript_excerpt = (transcript or "").strip()[:8000] or "(no transcript available)"
    content: list[dict] = [
        {
            "type": "text",
            "text": (
                f"Audio transcript:\n\n{transcript_excerpt}\n\n"
                f"{len(frames)} keyframes follow, numbered 0..{len(frames) - 1}:"
            ),
        },
    ]
    for frame in frames:
        b64 = base64.b64encode(frame.path.read_bytes()).decode("ascii")
        content.append({
            "type": "text",
            "text": f"Frame {frame.index} (t={frame.timestamp_seconds:.1f}s):",
        })
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": b64,
            },
        })
    content.append({
        "type": "text",
        "text": "Return strict JSON per the VisionAnalysis schema.",
    })

    response = await client.messages.parse(
        model=settings.vision_model,
        max_tokens=2048,
        system=[
            {
                "type": "text",
                "text": _VISION_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": content}],
        output_format=_VisionAnalysis,
    )
    record_anthropic_usage(response, kind="vision", model=settings.vision_model)

    if response.parsed_output is None:
        raise RuntimeError("vision call: parsed_output is None")
    return response.parsed_output
