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
import re
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
    # Phase 18: the transcript passage that motivated keeping this frame's
    # timestamp through the candidate filter. Empty string when no segment
    # ranking was available (empty transcript, ranking call failed) — the
    # vision call falls back to its pre-Phase-18 behavior in that case.
    motivation: str = ""


@dataclass
class _TranscriptSegment:
    """A speech segment with start/end seconds and the text spoken in that
    window. Unified across sources: Whisper `verbose_json` segments, parsed
    YouTube caption-paragraph markers, both feed into this shape."""

    start_seconds: float
    end_seconds: float
    text: str


@dataclass
class _RankedWindow:
    """Output of the transcript-ranking LLM call. Wraps a coalesced window
    of segments (one window typically spans 20-60s) with the scores assigned
    to that window. The window text is preserved so candidate frames inside
    the window can attach it as motivation for the downstream vision call."""

    start_seconds: float
    end_seconds: float
    text: str
    importance: int           # 0-10, "does this passage matter at all"
    visual_anchor: int        # 0-10, "is there likely a visual on screen here"


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
    whisper_segments: list[dict] | None = None,
) -> tuple[str | None, list[KeyframeRef]]:
    """Run the full pipeline. Returns (grounded_summary, keyframes).

    `grounded_summary` is None when the vision call fails — the orchestrator
    falls back to the text-only summarizer in that case. `keyframes` is
    empty when nothing reaches the importance threshold.

    `whisper_segments` (optional): when present, Phase 18 transcript-guided
    keyframe selection runs a cheap text-only LLM ranking pass on the
    speech timeline and uses the per-window `visual_anchor` scores to
    prune candidate keyframe timestamps BEFORE the expensive vision call.
    None / empty list / ranking-disabled → pipeline behaves exactly as
    before (pre-Phase-18 path).
    """
    # Phase 18: parse transcript segments early — the YT-captions path embeds
    # timestamp markers in the markdown body; the Whisper path passes them
    # explicitly via the `whisper_segments` kwarg. We unify both into the
    # `_TranscriptSegment` shape that the ranker consumes.
    transcript_segments = _build_transcript_segments(transcript, whisper_segments)

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

    # Phase 18: transcript-guided candidate filter. When we have a usable
    # transcript with timestamp info, ask a text-only LLM to score each
    # speech window for (importance, visual_anchor_likelihood), then prune
    # candidate frames whose window's visual_anchor score is too low. Each
    # surviving frame also picks up a `motivation` string — the passage the
    # speaker uttered in that window — which the vision call uses as
    # per-frame context for a sharper importance rating.
    if (
        settings.transcript_guided_selection_enabled
        and transcript_segments
        and _has_enough_words_for_ranking(transcript_segments)
    ):
        try:
            ranked = await _rank_transcript_segments(transcript_segments)
        except Exception as e:  # noqa: BLE001 — best-effort
            log.warning("video_analysis: transcript ranking failed (continuing without): %s", e)
            ranked = []
        if ranked:
            frames = _filter_frames_by_ranking(
                frames=frames,
                ranked_windows=ranked,
                visual_anchor_threshold=settings.transcript_visual_anchor_threshold,
                pure_visual_reserve_ratio=settings.transcript_pure_visual_reserve_ratio,
                max_frames=settings.max_frames_per_video,
            )
            log.info(
                "video_analysis: transcript-guided filter kept %d frames "
                "(visual_anchor_threshold=%d)",
                len(frames), settings.transcript_visual_anchor_threshold,
            )

    if not frames:
        log.info("video_analysis: transcript-guided filter dropped all frames; skipping vision call")
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


# ── Phase 18: transcript-guided keyframe selection ────────────────


# Matches the markdown anchor that `fetch_youtube_transcript` injects at
# the start of each ~30s paragraph: `[**0:42**](https://.../?v=ABC&t=42s) text`.
# The `t=Ns` group is the authoritative start-second value (`mm:ss` is
# display-only); we capture it as an integer count of seconds.
_YT_TRANSCRIPT_ANCHOR_RE = re.compile(
    r"\[\*\*\d+(?::\d{2}){1,2}\*\*\]\([^)]*?[?&]t=(\d+)s\)\s*"
)


def _build_transcript_segments(
    transcript: str,
    whisper_segments: list[dict] | None,
) -> list[_TranscriptSegment]:
    """Unify both transcript sources into the same shape.

    Order of preference:
      1. Explicit Whisper segments (verbose_json) when present.
      2. YouTube-caption markdown anchors parsed out of the transcript
         body — produced by `_youtube_transcript._format_transcript_with_timestamps`.
      3. Otherwise empty list: caller bypasses ranking.
    """
    if whisper_segments:
        out: list[_TranscriptSegment] = []
        for s in whisper_segments:
            try:
                start = float(s["start"])
                end = float(s["end"])
                text = (s.get("text") or "").strip()
            except (KeyError, TypeError, ValueError):
                continue
            if text:
                out.append(_TranscriptSegment(start_seconds=start, end_seconds=end, text=text))
        return out

    if not transcript:
        return []
    # Parse the YT-captions markdown shape:
    #   [**0:00**](url&t=0s) first paragraph text
    #   \n\n
    #   [**0:30**](url&t=30s) second paragraph text
    matches = list(_YT_TRANSCRIPT_ANCHOR_RE.finditer(transcript))
    if not matches:
        return []
    parsed: list[_TranscriptSegment] = []
    for i, m in enumerate(matches):
        try:
            start = float(m.group(1))
        except ValueError:
            continue
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(transcript)
        text = transcript[body_start:body_end].strip()
        # End of THIS paragraph is the start of the next anchor — or, for
        # the final paragraph, an estimate based on word count at ~150 wpm.
        if i + 1 < len(matches):
            try:
                end = float(matches[i + 1].group(1))
            except ValueError:
                end = start + max(1.0, len(text.split()) * 60.0 / 150.0)
        else:
            end = start + max(1.0, len(text.split()) * 60.0 / 150.0)
        if text:
            parsed.append(_TranscriptSegment(start_seconds=start, end_seconds=end, text=text))
    return parsed


def _has_enough_words_for_ranking(segments: list[_TranscriptSegment]) -> bool:
    """Skip ranking on transcripts that are too short to give the LLM any
    useful signal — saves a call on tiny/music/silent videos. The threshold
    is intentionally low: the ranking is robust to a few low-information
    windows, so we mostly want to weed out "(no transcript)" and
    near-empty cases."""
    total = sum(len(s.text.split()) for s in segments)
    return total >= settings.transcript_min_words_for_ranking


def _coalesce_segments_for_ranking(
    segments: list[_TranscriptSegment],
    window_seconds: float,
) -> list[_TranscriptSegment]:
    """Merge consecutive Whisper / caption segments into larger windows
    (~window_seconds each) so the ranking LLM gets readable chunks. Whisper
    segments are often 5-10 seconds — too fine-grained to rank usefully on
    their own ("this 8s window has no infographic" is too local). 30-60s
    windows give the LLM enough context to spot deictic markers."""
    if not segments:
        return []
    windows: list[_TranscriptSegment] = []
    current_start = segments[0].start_seconds
    current_end = segments[0].end_seconds
    current_parts: list[str] = [segments[0].text]
    for s in segments[1:]:
        if s.end_seconds - current_start <= window_seconds:
            current_end = s.end_seconds
            current_parts.append(s.text)
        else:
            windows.append(_TranscriptSegment(
                start_seconds=current_start,
                end_seconds=current_end,
                text=" ".join(current_parts).strip(),
            ))
            current_start = s.start_seconds
            current_end = s.end_seconds
            current_parts = [s.text]
    windows.append(_TranscriptSegment(
        start_seconds=current_start,
        end_seconds=current_end,
        text=" ".join(current_parts).strip(),
    ))
    return windows


class _RankedWindowOut(BaseModel):
    """LLM response shape for one window. The model is told to mirror
    `window_index` from the input so we can join scores back to windows
    without depending on response ordering."""

    window_index: int = Field(description="0-based index of the window as numbered in the input.")
    importance: int = Field(
        ge=0, le=10,
        description=(
            "0-10. How much does this passage matter to a reader who only sees "
            "a summary? 10 = key insight, surprising fact, actionable advice. "
            "0 = filler / pleasantries / repeating the previous point."
        ),
    )
    visual_anchor: int = Field(
        ge=0, le=10,
        description=(
            "0-10. How likely is it that a meaningful visual was on screen "
            "during this passage? Reward DEICTIC MARKERS ('as you can see', "
            "'look at this', 'here's', 'the chart shows', 'on screen', 'let "
            "me bring up'), NUMERICAL CLAIMS that beg for a chart, and TOPIC "
            "PIVOTS that often coincide with a new slide. 10 = the speaker "
            "is literally pointing at a slide/chart/UI/code. 0 = pure "
            "talking-head with no visual cue."
        ),
    )


class _RankingResult(BaseModel):
    """Full response: one entry per window, in window_index order."""

    windows: list[_RankedWindowOut]


_RANKING_SYSTEM_PROMPT = """You are scoring transcript windows from a video for an
automated keyframe-picking system.

You will be given N speech windows, each numbered with a `window_index` and a
`[start-end]s` time range. For EACH window, output two scores 0-10:

  importance (0-10) — How much does this passage matter to a reader who only
    sees a summary? 10 = key insight, surprising fact, actionable advice,
    explicit conclusion. 5 = useful context. 0 = filler, intro greetings,
    repeating an earlier point, mid-sentence pleasantries.

  visual_anchor (0-10) — How likely is it that a meaningful visual was on
    screen during this passage? Look for:
      • Deictic markers: "as you can see", "look at this", "here", "this
        shows", "the chart", "on screen", "let me bring up", "as shown".
      • Numerical specificity that begs for a chart: "47.3%", "the second
        result was X", explicit comparisons.
      • Topic pivots: "now, the interesting part" — new slide cues in
        screencasts.
    Score 10 when the speaker is literally pointing at a slide/chart/UI.
    Score 0 for pure talking-head passages with no visual cue.

Return strict JSON matching the RankingResult schema. ONE entry per input
window, in window_index order. Do not invent extra entries.
"""


async def _rank_transcript_segments(
    segments: list[_TranscriptSegment],
) -> list[_RankedWindow]:
    """Single text-only Claude call. Returns the ranked windows in time
    order. Errors propagate up — caller treats them as "no ranking" and
    falls back to the pre-Phase-18 path."""
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY missing — cannot run transcript ranking")

    windows = _coalesce_segments_for_ranking(
        segments,
        window_seconds=settings.transcript_ranking_window_seconds,
    )
    if not windows:
        return []

    # Build the user message. Cap at a reasonable count to keep one ranking
    # call cheap on hour-long videos. 80 windows × ~45s ≈ 60 minutes.
    cap = max(1, settings.transcript_ranking_max_windows)
    if len(windows) > cap:
        log.info(
            "video_analysis: transcript ranking capped at %d / %d windows",
            cap, len(windows),
        )
        windows = windows[:cap]

    listing_lines: list[str] = []
    for i, w in enumerate(windows):
        # Truncate per-window text so a runaway window doesn't dominate the
        # token budget; the ranker just needs enough context to spot
        # deictic markers, not the full transcript verbatim.
        snippet = w.text[: settings.transcript_ranking_window_chars].replace("\n", " ").strip()
        listing_lines.append(
            f"[window_index={i}] [{w.start_seconds:.1f}s-{w.end_seconds:.1f}s] {snippet}"
        )
    user_text = (
        f"Score these {len(windows)} transcript windows. Output one "
        f"RankedWindowOut per window, in window_index order.\n\n"
        + "\n\n".join(listing_lines)
    )

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    response = await client.messages.parse(
        model=settings.vision_model,
        max_tokens=2048,
        system=[
            {
                "type": "text",
                "text": _RANKING_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_text}],
        output_format=_RankingResult,
    )
    if response.parsed_output is None:
        raise RuntimeError("transcript ranking: parsed_output is None")

    by_idx = {w.window_index: w for w in response.parsed_output.windows}
    out: list[_RankedWindow] = []
    for i, w in enumerate(windows):
        scored = by_idx.get(i)
        if scored is None:
            # Model dropped a window; treat as middling so we neither force-keep
            # nor force-drop frames inside it.
            importance = 5
            visual_anchor = 5
        else:
            importance = scored.importance
            visual_anchor = scored.visual_anchor
        out.append(_RankedWindow(
            start_seconds=w.start_seconds,
            end_seconds=w.end_seconds,
            text=w.text,
            importance=importance,
            visual_anchor=visual_anchor,
        ))
    return out


def _window_for_timestamp(
    timestamp: float, windows: list[_RankedWindow],
) -> _RankedWindow | None:
    """Locate the ranked window covering `timestamp`. Linear scan — the
    window count is small (≤ transcript_ranking_max_windows, default 80)
    so a binary search isn't worth the complexity. Returns None when the
    timestamp falls outside all windows (rare; happens when the candidate
    landed in a silence gap not covered by Whisper)."""
    for w in windows:
        if w.start_seconds <= timestamp <= w.end_seconds:
            return w
    return None


def _filter_frames_by_ranking(
    *,
    frames: list[_ExtractedFrame],
    ranked_windows: list[_RankedWindow],
    visual_anchor_threshold: int,
    pure_visual_reserve_ratio: float,
    max_frames: int,
) -> list[_ExtractedFrame]:
    """Apply Phase 18's two-stage candidate filter.

    Stage 1: Keep frames whose ranked window has `visual_anchor >=
    threshold`. These are speech-anchored picks — moments where the
    speaker probably gestured at a visual.

    Stage 2: Reserve a fraction of `max_frames` (default 20%) for the
    HIGHEST-importance frames REGARDLESS of visual_anchor score. This is
    the B-roll safety net: a documentary with strong voiceover but
    high-importance unmentioned imagery shouldn't lose every frame.

    Frames in silent gaps (no covering window) get neither score — they
    survive only if the speech-anchored pool is empty (true fallback path).
    """
    # Score each frame by (visual_anchor, importance) from its window.
    scored: list[tuple[_ExtractedFrame, int, int, _RankedWindow | None]] = []
    for f in frames:
        w = _window_for_timestamp(f.timestamp_seconds, ranked_windows)
        if w is None:
            scored.append((f, -1, -1, None))
        else:
            scored.append((f, w.visual_anchor, w.importance, w))

    # Stage 1 — speech-anchored picks.
    speech_anchored = [
        s for s in scored if s[1] >= visual_anchor_threshold
    ]
    speech_anchored.sort(key=lambda s: (-s[1], -s[2]))

    # Stage 2 — pure-visual reserve, filled from the highest-importance
    # frames that DIDN'T already make Stage 1.
    reserve_quota = max(0, int(round(max_frames * pure_visual_reserve_ratio)))
    chosen_ts = {id(s[0]) for s in speech_anchored[: max(0, max_frames - reserve_quota)]}
    remainder = [
        s for s in scored
        if id(s[0]) not in chosen_ts
    ]
    # When the speech-anchored pool fills the whole budget, the reserve
    # collapses to 0 — that's fine, we already have a strong selection.
    remainder.sort(key=lambda s: -s[2])  # importance, descending

    out_frames: list[_ExtractedFrame] = []
    for s in speech_anchored[: max(0, max_frames - reserve_quota)]:
        f, _, _, w = s
        if w is not None:
            f.motivation = _summarize_window_text(w.text)
        out_frames.append(f)
    for s in remainder[:reserve_quota]:
        f, _, _, w = s
        if w is not None:
            f.motivation = _summarize_window_text(w.text)
        out_frames.append(f)

    # If BOTH pools came up empty (low-anchor video that also has no
    # importance signal), fall back to keeping the top frames by raw
    # importance score so the vision call still has something to look at.
    if not out_frames:
        scored.sort(key=lambda s: -s[2])
        for s in scored[:max_frames]:
            f, _, _, w = s
            if w is not None:
                f.motivation = _summarize_window_text(w.text)
            out_frames.append(f)

    out_frames.sort(key=lambda f: f.timestamp_seconds)
    return out_frames


def _summarize_window_text(text: str, max_chars: int = 200) -> str:
    """Trim a transcript window down to a short motivation string for the
    vision call. We just want the speaker's actual words near the timestamp
    — no LLM call needed. Heuristic: keep the first ~max_chars and break on
    the nearest word boundary so we don't end mid-token."""
    cleaned = " ".join(text.split())
    if len(cleaned) <= max_chars:
        return cleaned
    cut = cleaned[: max_chars]
    space = cut.rfind(" ")
    if space > max_chars * 0.6:
        cut = cut[:space]
    return cut + "…"


# ── Stage 1+2: scene detect + ffmpeg extract ──────────────────────


def _detect_and_extract_frames(
    video_path: Path,
    workdir: Path,
    max_frames: int,
) -> list[_ExtractedFrame]:
    """Synchronous: scene detect (PySceneDetect OR ffmpeg scdet) → ffmpeg
    seek+extract per scene midpoint."""
    algorithm = settings.scenedetect_algorithm.lower().strip()
    if algorithm == "ffmpeg_scdet":
        scene_timestamps = _detect_scene_timestamps_ffmpeg(video_path)
    elif algorithm == "transnetv2":
        scene_timestamps = _detect_scene_timestamps_transnetv2(video_path)
    else:
        scene_timestamps = _detect_scene_timestamps_pyscenedetect(video_path)

    if not scene_timestamps:
        # No scene cuts detected by the configured engine. Fall back to
        # fixed-interval samples (25%, 50%, 75% of video duration) for
        # short / single-shot videos.
        return _fallback_fixed_interval_frames(video_path, workdir, n=3)

    # Build the candidate timestamp list. Scene cuts go in first (visual
    # structure is the strongest signal); silencedetect midpoints, if
    # enabled, augment for talky content where the visual detector misses
    # topic boundaries (long screencasts, lectures, talking-head podcasts).
    candidates: list[float] = list(scene_timestamps)
    if settings.frame_silence_cuts_enabled:
        silence_timestamps = _silencedetect_timestamps(video_path)
        if silence_timestamps:
            log.info(
                "video_analysis: silencedetect contributed %d candidate timestamps",
                len(silence_timestamps),
            )
            candidates = _merge_dedup_timestamps(
                primary=candidates,
                secondary=silence_timestamps,
                min_gap_seconds=settings.frame_candidate_dedup_seconds,
            )

    # Cap at max_frames, spreading the picks evenly across the FULL candidate
    # list (endpoints included). `int(i * step)` consistently lost the tail
    # because indices were always [0, step, 2*step, …]. `np.linspace(0, N-1,
    # max_frames)` rounded to int hits both endpoints and stays unbiased.
    candidates.sort()
    if len(candidates) > max_frames:
        idxs = np.linspace(0, len(candidates) - 1, max_frames, dtype=int)
        # np.linspace can repeat the same index near the endpoints when
        # max_frames is close to len(candidates); dedup while preserving order.
        seen: set[int] = set()
        ordered_unique: list[int] = []
        for i in idxs.tolist():
            if i not in seen:
                seen.add(i)
                ordered_unique.append(i)
        picked_timestamps = [candidates[i] for i in ordered_unique]
    else:
        picked_timestamps = candidates

    # Build the extract task list from the chosen timestamps.
    tasks: list[tuple[int, float, Path]] = [
        (idx, ts, workdir / f"frame-{idx:02d}.jpg")
        for idx, ts in enumerate(picked_timestamps)
    ]

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


def _detect_scene_timestamps_pyscenedetect(video_path: Path) -> list[float]:
    """PySceneDetect path. Returns scene MIDPOINTS as the candidate sampling
    timestamps. Honors the algorithm + sensitivity-retry logic configured
    via settings."""
    try:
        from scenedetect import detect, AdaptiveDetector, ContentDetector
    except ImportError as e:
        log.warning("scenedetect not installed: %s", e)
        return []

    def _build_detector(sensitivity: float):
        algorithm = settings.scenedetect_algorithm.lower().strip()
        if algorithm == "content":
            return ContentDetector(
                threshold=settings.scenedetect_threshold * sensitivity,
                min_scene_len=settings.scenedetect_min_scene_len,
                luma_only=settings.scenedetect_luma_only,
            )
        if algorithm not in ("adaptive", ""):
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
    if not scene_list:
        # First pass turned up nothing → retry once with halved thresholds
        # before the orchestrator falls back to fixed intervals.
        log.info("video_analysis: 0 scenes at default sensitivity; retrying at 0.5x")
        scene_list = detect(str(video_path), _build_detector(0.5))

    return [(s.get_seconds() + e.get_seconds()) / 2.0 for s, e in scene_list]


def _detect_scene_timestamps_ffmpeg(video_path: Path) -> list[float]:
    """ffmpeg `scdet` path. Several times faster than PySceneDetect; weaker on
    slow dissolves. Returns scene MIDPOINTS derived from the cut times +
    duration boundaries (t=0 and t=duration are implicit boundaries)."""
    cuts = _scdet_cut_times(video_path)
    duration = _video_duration_seconds(video_path)
    if duration <= 0:
        # No way to compute the final scene's midpoint without duration.
        # Fall back to cut times themselves (rough but better than nothing).
        return list(cuts)
    return _cuts_to_scene_midpoints(cuts, duration)


def _detect_scene_timestamps_transnetv2(video_path: Path) -> list[float]:
    """TransNetV2 path. CNN+LSTM shot-boundary detector — materially better
    on slow dissolves than PySceneDetect or ffmpeg `scdet`. Returns scene
    MIDPOINTS computed from the model's per-scene (start, end) ranges.

    Optional dependency. When `transnetv2-pytorch` is not installed, returns
    [] so the caller falls back to the fixed-interval sampling path (same
    behavior as PySceneDetect missing — operator gets a warning, never a
    crash).

    Auto-selects CUDA when `torch.cuda.is_available()`, otherwise CPU. The
    7-layer model is small enough that CPU inference is tractable for short
    clips even though GPU is recommended for video > ~5 minutes.
    """
    try:
        import torch
        from transnetv2_pytorch import TransNetV2
    except ImportError as e:
        log.warning(
            "transnetv2-pytorch not installed; install via "
            "`pip install transnetv2-pytorch`. Returning empty so caller "
            "falls back to the fixed-interval path: %s",
            e,
        )
        return []

    weights_path = (settings.scenedetect_transnet_weights or "").strip() or None
    threshold = settings.scenedetect_transnet_threshold

    try:
        model = TransNetV2()
        if weights_path:
            # Operator pinned a specific weights file (vendored asset or
            # read-only mount). Load it explicitly. Otherwise we trust the
            # package to resolve / download its own weights.
            state = torch.load(weights_path, map_location="cpu")
            model.load_state_dict(state)
        if torch.cuda.is_available():
            model = model.cuda()
        model.eval()

        # The maintained `transnetv2-pytorch` PyPI package exposes a
        # `detect_scenes(video_path, threshold=...)` convenience method
        # that handles frame decoding + 100-frame chunking internally.
        # The upstream `soCzech/TransNetV2` repo's `inference-pytorch`
        # only ships the raw model forward — operators using that path
        # get a clear warning and graceful fallback rather than a crash.
        if not hasattr(model, "detect_scenes"):
            log.warning(
                "transnetv2 model has no detect_scenes() method — use the "
                "maintained PyPI package (`pip install transnetv2-pytorch`). "
                "Returning empty."
            )
            return []
        with torch.no_grad():
            scenes = model.detect_scenes(str(video_path), threshold=threshold)
    except Exception as e:  # noqa: BLE001 — best-effort
        log.warning("transnetv2 detection failed: %s", e)
        return []

    midpoints: list[float] = []
    for scene in scenes or []:
        try:
            start = float(scene["start_time"])
            end = float(scene["end_time"])
        except (KeyError, TypeError, ValueError):
            continue
        midpoints.append((start + end) / 2.0)
    return midpoints


def _fallback_fixed_interval_frames(
    video_path: Path,
    workdir: Path,
    n: int = 3,
) -> list[_ExtractedFrame]:
    """When scene detection finds nothing (single-shot videos), sample at
    fixed 25/50/75% positions of duration."""
    duration = _video_duration_seconds(video_path)
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
        cmd = ["ffmpeg", *_hwaccel_args(),
            "-ss", f"{seek_start:.3f}",
            "-i", str(video_path),
            "-t", f"{thumbnail_window_seconds:.3f}",
            "-an", "-sn", "-dn",
        ]
    else:
        # Legacy fast path: single-frame seek at the exact timestamp.
        cmd = ["ffmpeg", *_hwaccel_args(),
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


# ── Audio-based cut detection (silencedetect) ────────────────────────


_SILENCE_END_RE = re.compile(
    r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)"
)
# scdet emits `lavfi.scd.time: 12.345` (sometimes with a colon, sometimes
# with a comma-separated `score: …, time: …` form depending on the ffmpeg
# build). One permissive regex catches both.
_SCDET_TIME_RE = re.compile(r"lavfi\.scd\.time:?\s*([0-9.]+)")


def _silencedetect_timestamps(video_path: Path) -> list[float]:
    """Run ffmpeg `silencedetect` on the audio track and return the midpoints
    of each detected silence interval as candidate sampling timestamps. The
    midpoint (silence_end - silence_duration/2) is the most likely place to
    find a representative frame for the speech segment that just concluded.

    Returns [] on any error; the caller treats this as "no silence signal".
    """
    threshold_db = settings.frame_silence_threshold_db
    min_duration = settings.frame_silence_min_duration
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i", str(video_path),
        "-af", f"silencedetect=noise={threshold_db}dB:d={min_duration}",
        "-vn",            # don't decode video — audio analysis only
        "-f", "null",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("silencedetect failed: %s", e)
        return []
    if proc.returncode != 0:
        log.warning(
            "silencedetect returned %d; stderr (last 500B): %s",
            proc.returncode,
            (proc.stderr or b"")[-500:].decode("utf-8", errors="replace"),
        )
        return []

    out: list[float] = []
    stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
    for match in _SILENCE_END_RE.finditer(stderr):
        try:
            end = float(match.group(1))
            duration = float(match.group(2))
        except ValueError:
            continue
        midpoint = max(0.0, end - duration / 2.0)
        out.append(midpoint)
    return out


def _merge_dedup_timestamps(
    *,
    primary: list[float],
    secondary: list[float],
    min_gap_seconds: float,
) -> list[float]:
    """Merge two timestamp lists. `primary` entries are always retained;
    `secondary` entries are added only when no already-retained timestamp is
    within `min_gap_seconds`. Caller is responsible for any final sorting."""
    accepted = list(primary)
    for ts in secondary:
        if all(abs(ts - kept) >= min_gap_seconds for kept in accepted):
            accepted.append(ts)
    return accepted


def _video_duration_seconds(video_path: Path) -> float:
    """ffprobe-based duration lookup. Returns 0.0 on any failure.
    Shared between the scdet pre-pass (needs duration to compute the final
    scene's midpoint) and the fixed-interval fallback path."""
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return 0.0
    if proc.returncode != 0:
        return 0.0
    try:
        return float((proc.stdout or "").strip())
    except ValueError:
        return 0.0


def _scdet_cut_times(video_path: Path) -> list[float]:
    """Run ffmpeg's `scdet` filter and return the list of detected cut times.
    Several times faster than PySceneDetect — decode is in-process and no
    OpenCV round-trip — at the cost of weaker behavior on slow dissolves.

    Returns [] on any failure; caller falls back to the fixed-interval path
    or to the silence-cut signal as configured.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        *_hwaccel_args(),
        "-i", str(video_path),
        "-vf", f"scdet=threshold={settings.scenedetect_ffmpeg_threshold}",
        "-an", "-sn", "-dn",
        "-f", "null",
        "-",
    ]
    try:
        # scdet decodes the whole video — give it more headroom than the
        # per-frame extract timeout. Capped so a malicious / corrupt file
        # can't hang the worker indefinitely.
        proc = subprocess.run(cmd, capture_output=True, check=False, timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("ffmpeg scdet failed: %s", e)
        return []
    if proc.returncode != 0:
        log.warning(
            "ffmpeg scdet returned %d; stderr (last 500B): %s",
            proc.returncode,
            (proc.stderr or b"")[-500:].decode("utf-8", errors="replace"),
        )
        return []

    stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
    out: list[float] = []
    for m in _SCDET_TIME_RE.finditer(stderr):
        try:
            out.append(float(m.group(1)))
        except ValueError:
            continue
    return out


def _cuts_to_scene_midpoints(cuts: list[float], duration: float) -> list[float]:
    """Convert a list of cut TIMES (instants where the scene changes) into the
    list of scene MIDPOINTS, treating t=0 and t=duration as implicit
    boundaries. A cut at t=T splits the timeline into a "before" scene ending
    at T and an "after" scene starting at T. Midpoints are the best single
    representative timestamp per scene."""
    if duration <= 0:
        return []
    boundaries = sorted({0.0, duration, *(c for c in cuts if 0.0 < c < duration)})
    midpoints: list[float] = []
    for start, end in zip(boundaries[:-1], boundaries[1:]):
        midpoints.append((start + end) / 2.0)
    return midpoints


def _hwaccel_args() -> list[str]:
    """Return `["-hwaccel", value]` if the operator enabled hardware decode,
    else []. Keeping this in one place so every ffmpeg invocation in this
    module picks it up consistently — extract, future scdet pre-pass, etc."""
    val = (settings.ffmpeg_hwaccel or "").strip()
    return ["-hwaccel", val] if val else []


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
        # Phase 18: when transcript-guided selection chose this frame, attach
        # the speech window that motivated it as inline context for the
        # vision call. Sharpens the importance rating by telling the model
        # WHY this timestamp was singled out (e.g. "speaker said 'as you
        # can see in this chart'").
        header = f"Frame {frame.index} (t={frame.timestamp_seconds:.1f}s):"
        if frame.motivation:
            header += f"\nSpoken nearby: {frame.motivation}"
        content.append({"type": "text", "text": header})
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
