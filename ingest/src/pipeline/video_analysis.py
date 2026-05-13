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
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field

from src.config import settings
from src.pipeline.video_analysis_filters import filter_low_quality_frames

log = logging.getLogger(__name__)


@dataclass
class KeyframeRef:
    """A keyframe that's been uploaded to AFFiNE and is ready to embed."""

    blob_source_id: str
    caption: str
    timestamp_seconds: float
    # Reserved for Tier-3 OCR: when set, the transcribed text from the frame
    # (slide content, code on screen). Defaulted to None so existing callers
    # and JSONB snapshots remain compatible.
    ocr_text: str | None = None


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

    # Stage 3 (resize) is now folded into ffmpeg extract via the `-vf scale=`
    # filter in `_ffmpeg_extract_frame`. Frames arrive at the target long-edge
    # already, so the Pillow re-encode loop has been retired.

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
    try:
        from scenedetect import detect, AdaptiveDetector, ContentDetector
    except ImportError as e:
        log.warning("scenedetect not installed: %s", e)
        return []

    def _build_detector(sensitivity: float):
        """sensitivity multiplier <1 → more sensitive (lower thresholds → more cuts).
        Used by the retry path to try again with a more permissive setup before
        we give up and fall back to fixed-interval samples."""
        algorithm = settings.scenedetect_algorithm.lower().strip()
        if algorithm == "content":
            return ContentDetector(
                threshold=settings.scenedetect_threshold * sensitivity,
                min_scene_len=settings.scenedetect_min_scene_len,
                luma_only=settings.scenedetect_luma_only,
            )
        if algorithm != "adaptive":
            log.warning(
                "video_analysis: unknown scenedetect_algorithm=%r — falling back to 'adaptive'",
                settings.scenedetect_algorithm,
            )
        return AdaptiveDetector(
            adaptive_threshold=settings.scenedetect_adaptive_threshold * sensitivity,
            min_content_val=settings.scenedetect_threshold * sensitivity,
            min_scene_len=settings.scenedetect_min_scene_len,
            luma_only=settings.scenedetect_luma_only,
        )

    scene_list = detect(str(video_path), _build_detector(1.0))

    # First pass found nothing → retry once at half thresholds before
    # giving up and falling back to fixed intervals. Common case: a
    # documentary with slow dissolves where the default threshold is
    # just too conservative. Half-threshold gives us a second chance
    # at structure-driven sampling.
    if not scene_list:
        log.info("video_analysis: 0 scenes at default sensitivity; retrying at 0.5x")
        scene_list = detect(str(video_path), _build_detector(0.5))

    if not scene_list:
        # No scene cuts detected. Fall back to fixed-interval samples
        # (25%, 50%, 75% of video duration) for short / single-shot videos.
        return _fallback_fixed_interval_frames(video_path, workdir, n=3)

    # Cap at max_frames, spreading the picks evenly across the FULL scene
    # list (endpoints included). `int(i * step)` consistently lost the tail
    # because indices were always [0, step, 2*step, …]. `np.linspace(0, N-1,
    # max_frames)` rounded to int hits both endpoints and stays unbiased.
    if len(scene_list) > max_frames:
        idxs = np.linspace(0, len(scene_list) - 1, max_frames, dtype=int)
        # np.linspace can repeat the same index near the endpoints when
        # max_frames is close to len(scene_list); dedup while preserving order.
        seen: set[int] = set()
        ordered_unique: list[int] = []
        for i in idxs.tolist():
            if i not in seen:
                seen.add(i)
                ordered_unique.append(i)
        picks = [scene_list[i] for i in ordered_unique]
    else:
        picks = scene_list

    # Build the extract task list. Sample the MIDDLE of each scene, not the
    # cut itself — the first frame after a cut is frequently a partial fade,
    # motion-blurred, or a transitional thumbnail.
    tasks: list[tuple[int, float, Path]] = []
    for idx, (start, end) in enumerate(picks):
        ts = (start.get_seconds() + end.get_seconds()) / 2.0
        tasks.append((idx, ts, workdir / f"frame-{idx:02d}.jpg"))

    # Run ffmpeg extracts in parallel. Each subprocess.run releases the GIL
    # while waiting on the child process, so a ThreadPoolExecutor gives a
    # near-linear speedup on multi-core hosts. Bounded so we don't spawn
    # 12 simultaneous ffmpegs on a 2-core box. asyncio.to_thread already
    # wraps this whole function, so blocking here doesn't block the event loop.
    def _do_extract(task: tuple[int, float, Path]) -> tuple[int, float, Path] | None:
        idx, ts, path = task
        ok = _ffmpeg_extract_frame(
            video_path,
            ts,
            path,
            long_edge_px=settings.frame_long_edge_px,
            thumbnail_window_seconds=settings.frame_thumbnail_window_seconds,
        )
        return (idx, ts, path) if ok else None

    worker_count = max(1, min(len(tasks), settings.frame_extract_workers))
    if worker_count == 1 or len(tasks) <= 1:
        results = [_do_extract(t) for t in tasks]
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            results = list(pool.map(_do_extract, tasks))

    out: list[_ExtractedFrame] = []
    for r in results:
        if r is None:
            continue
        idx, ts, path = r
        out.append(_ExtractedFrame(path=path, timestamp_seconds=ts, index=idx))
    # Keep chronological order even if extracts finished out of order.
    out.sort(key=lambda f: f.timestamp_seconds)
    return out


def _fallback_fixed_interval_frames(
    video_path: Path,
    workdir: Path,
    n: int = 3,
) -> list[_ExtractedFrame]:
    """When scene detection finds nothing (single-shot videos), sample at
    fixed 25/50/75% positions of duration."""
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
        if _ffmpeg_extract_frame(
            video_path, ts, frame_path, long_edge_px=settings.frame_long_edge_px,
        ):
            out.append(_ExtractedFrame(path=frame_path, timestamp_seconds=ts, index=i))
    return out


def _ffmpeg_extract_frame(
    video_path: Path,
    timestamp_seconds: float,
    out_path: Path,
    long_edge_px: int | None = None,
    thumbnail_window_seconds: float = 0.0,
) -> bool:
    """Single-frame extract via ffmpeg. Returns True on success.

    When `long_edge_px` is set, the frame is scaled down so its long edge fits
    within that pixel count in the same ffmpeg invocation — avoiding the
    extract → JPEG → Pillow → JPEG double-encode that was the previous design.
    Pure downscale: a frame already smaller than the box is left untouched.

    When `thumbnail_window_seconds > 0`, ffmpeg decodes a small window centered
    on `timestamp_seconds` and uses the `thumbnail` filter to pick the most
    visually distinct frame from that window — much more robust than the
    exact-midpoint seek when the midpoint happens to land on motion blur
    or a transitional sub-frame.
    """
    # Build the -vf filter chain (thumbnail first if requested, then scale).
    vf_parts: list[str] = []
    if thumbnail_window_seconds > 0:
        # batch=999 ensures `thumbnail` emits exactly one output for any
        # reasonable window size (decodes ≲ a few dozen frames per scene).
        vf_parts.append("thumbnail=999")
    if long_edge_px is not None and long_edge_px > 0:
        # Cap each axis at `long_edge_px` and let ffmpeg pick the matching
        # dimension on the OTHER axis via force_original_aspect_ratio=decrease.
        # force_divisible_by=2 keeps mjpeg/h264-friendly even dimensions.
        vf_parts.append(
            f"scale='min({long_edge_px},iw)':'min({long_edge_px},ih)'"
            ":force_original_aspect_ratio=decrease"
            ":force_divisible_by=2:flags=lanczos"
        )

    if thumbnail_window_seconds > 0:
        # Decode a small window centered on the requested timestamp.
        seek_start = max(0.0, timestamp_seconds - thumbnail_window_seconds / 2.0)
        cmd = [
            "ffmpeg",
            "-ss", f"{seek_start:.3f}",
            "-i", str(video_path),
            "-t", f"{thumbnail_window_seconds:.3f}",
            "-an", "-sn", "-dn",
        ]
    else:
        # Legacy fast path: single-frame seek at the exact timestamp.
        cmd = [
            "ffmpeg",
            # -ss BEFORE -i is much faster (seek demuxer) but slightly less
            # precise. For keyframe captures that's fine.
            "-ss", f"{timestamp_seconds:.3f}",
            "-i", str(video_path),
            "-an", "-sn", "-dn",
        ]
    if vf_parts:
        cmd += ["-vf", ",".join(vf_parts)]
    cmd += [
        # Cap output at 1 frame. With the thumbnail filter this is
        # belt-and-suspenders against pathological batches; without it,
        # it's how we tell ffmpeg "just one image, not the whole stream".
        "-frames:v", "1",
        "-q:v", "2",  # high JPEG quality, ~80% smaller than PNG
        "-y",         # overwrite
        str(out_path),
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, check=False, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("ffmpeg extract failed at t=%.2fs: %s", timestamp_seconds, e)
        return False

    if proc.returncode != 0:
        log.warning(
            "ffmpeg extract returned %d at t=%.2fs; stderr (last 500B): %s",
            proc.returncode,
            timestamp_seconds,
            (proc.stderr or b"")[-500:].decode("utf-8", errors="replace"),
        )
        return False
    if not (out_path.is_file() and out_path.stat().st_size > 0):
        return False
    # Header-level validation. ffmpeg can occasionally return rc=0 while
    # producing a truncated JPEG (especially mid-thumbnail-batch); Pillow's
    # `verify()` parses the chunks without decoding pixels — cheap and
    # catches the corruption case that previously blew up downstream in
    # the resize / phash path.
    if not _is_valid_jpeg(out_path):
        log.warning("ffmpeg produced an invalid JPEG at t=%.2fs", timestamp_seconds)
        return False
    return True


def _is_valid_jpeg(path: Path) -> bool:
    """True if the file at `path` parses as a structurally-valid JPEG.
    `Image.verify()` only walks the headers; it does NOT decode pixels."""
    try:
        from PIL import Image
    except ImportError:
        # If Pillow is somehow missing, fall back to "any non-empty file
        # is good enough" rather than rejecting everything.
        return True
    try:
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:  # noqa: BLE001 — best-effort
        return False


# ── Stage 3: Pillow resize (retired — kept as a no-op shim) ───────


def _resize_frames_in_place(frames: list[_ExtractedFrame], long_edge_px: int) -> None:
    """Deprecated. Resize is now done by ffmpeg's `-vf scale=…` filter
    inside `_ffmpeg_extract_frame`, eliminating the lossy JPEG re-encode
    that this function used to perform. Retained as a no-op so that
    existing test monkeypatch sites keep resolving."""
    del frames, long_edge_px  # explicit unused


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

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

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

    if response.parsed_output is None:
        raise RuntimeError("vision call: parsed_output is None")
    return response.parsed_output
