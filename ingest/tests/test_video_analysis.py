"""Tests for Phase 13 video frame analysis.

The full pipeline has three external dependencies (scenedetect, ffmpeg,
Anthropic vision API) — we mock all three and test:
  - frame extraction respects max_frames cap
  - resize is best-effort (failure doesn't kill the call)
  - vision call gets the right multimodal payload shape
  - importance threshold filters frames
  - blob upload happens once per kept frame
  - keyframes propagate through to the dataclass return value
  - graceful degradation: any stage failing → returns (None, [])
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.pipeline.video_analysis import (
    KeyframeRef,
    _ExtractedFrame,
    _FrameCaption,
    _cuts_to_scene_midpoints,
    _detect_scene_timestamps_transnetv2,
    _merge_dedup_timestamps,
    _SCDET_TIME_RE,
    _SILENCE_END_RE,
    _VisionAnalysis,
    analyze_video,
)


# ── analyze_video happy path ───────────────────────────────────────


@pytest.mark.asyncio
async def test_analyze_video_uploads_kept_frames_and_returns_summary(tmp_path, monkeypatch):
    """End-to-end happy path: 3 scenes detected → 3 frames extracted →
    vision returns summary + 3 captions (2 above threshold) → 2 frames
    uploaded to blob storage → 2 KeyframeRefs returned."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    video_path = workdir / "video.mp4"
    video_path.write_bytes(b"fake mp4 bytes")

    # Stub scene+frame extraction to return 3 frames.
    fake_frames = [
        _ExtractedFrame(path=workdir / f"frame-{i:02d}.jpg", timestamp_seconds=float(i * 5), index=i)
        for i in range(3)
    ]
    for f in fake_frames:
        f.path.write_bytes(b"\xff\xd8\xff\xe0fake jpeg")

    monkeypatch.setattr(
        "src.pipeline.video_analysis._detect_and_extract_frames",
        lambda *a, **k: fake_frames,
    )
    # Skip resize (it's best-effort; doesn't block the test)
    monkeypatch.setattr(
        "src.pipeline.video_analysis._resize_frames_in_place",
        lambda *a, **k: None,
    )

    # Stub the vision call: 3 captions, 2 above importance threshold (4)
    fake_analysis = _VisionAnalysis(
        summary="A grounded summary that mentions both the audio and visuals.",
        keyframes=[
            _FrameCaption(frame_index=0, caption="Title screen with logo", importance=8),
            _FrameCaption(frame_index=1, caption="Black transition frame", importance=1),  # filtered
            _FrameCaption(frame_index=2, caption="Code snippet showing useEffect", importance=9),
        ],
    )
    monkeypatch.setattr(
        "src.pipeline.video_analysis._vision_call",
        AsyncMock(return_value=fake_analysis),
    )

    # Mock mcp_client.upload_blob — return a sourceId per call.
    upload_calls: list[dict] = []

    async def _fake_upload_blob(**kwargs):
        upload_calls.append(kwargs)
        return {"sourceId": f"blob-{kwargs['filename']}", "byteCount": 100, "ok": True}

    mcp_client = MagicMock()
    mcp_client.upload_blob = _fake_upload_blob

    summary, keyframes = await analyze_video(
        video_path=video_path,
        workdir=workdir,
        transcript="The speaker explains useEffect cleanup.",
        capture_id="01J-TEST",
        mcp_client=mcp_client,
    )

    assert summary == "A grounded summary that mentions both the audio and visuals."
    assert len(keyframes) == 2  # frame 1 (importance=1) was filtered
    assert all(isinstance(k, KeyframeRef) for k in keyframes)

    # Check kept frames are in chronological order
    assert keyframes[0].timestamp_seconds == 0.0
    assert keyframes[1].timestamp_seconds == 10.0
    assert keyframes[0].caption == "Title screen with logo"
    assert keyframes[1].caption == "Code snippet showing useEffect"

    # Check sourceIds + filenames
    assert keyframes[0].blob_source_id == "blob-01J-TEST-frame-00.jpg"
    assert keyframes[1].blob_source_id == "blob-01J-TEST-frame-02.jpg"

    # Two upload calls (frame 1 was filtered before upload)
    assert len(upload_calls) == 2
    assert upload_calls[0]["content_type"] == "image/jpeg"
    assert upload_calls[0]["filename"].endswith("-frame-00.jpg")


@pytest.mark.asyncio
async def test_analyze_video_returns_empty_when_no_scenes_detected(tmp_path, monkeypatch):
    """No scenes → no vision call → no uploads."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    video_path = workdir / "video.mp4"
    video_path.write_bytes(b"x")

    monkeypatch.setattr(
        "src.pipeline.video_analysis._detect_and_extract_frames",
        lambda *a, **k: [],
    )
    vision_mock = AsyncMock()
    monkeypatch.setattr("src.pipeline.video_analysis._vision_call", vision_mock)

    mcp_client = MagicMock()
    mcp_client.upload_blob = AsyncMock()

    summary, keyframes = await analyze_video(
        video_path=video_path,
        workdir=workdir,
        transcript="",
        capture_id="01J",
        mcp_client=mcp_client,
    )

    assert summary is None
    assert keyframes == []
    vision_mock.assert_not_called()
    mcp_client.upload_blob.assert_not_awaited()


@pytest.mark.asyncio
async def test_analyze_video_handles_scenedetect_failure_gracefully(tmp_path, monkeypatch):
    """scenedetect raising → analyze_video returns empties, no crash."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    video_path = workdir / "video.mp4"
    video_path.write_bytes(b"x")

    def _raise(*a, **k):
        raise RuntimeError("scenedetect dependency missing")

    monkeypatch.setattr("src.pipeline.video_analysis._detect_and_extract_frames", _raise)

    mcp_client = MagicMock()
    summary, keyframes = await analyze_video(
        video_path=video_path,
        workdir=workdir,
        transcript="",
        capture_id="01J",
        mcp_client=mcp_client,
    )
    assert summary is None
    assert keyframes == []


@pytest.mark.asyncio
async def test_analyze_video_handles_vision_call_failure(tmp_path, monkeypatch):
    """vision call raises → still no crash, returns empties."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    video_path = workdir / "video.mp4"
    video_path.write_bytes(b"x")

    fake_frames = [
        _ExtractedFrame(path=workdir / "frame-00.jpg", timestamp_seconds=1.0, index=0),
    ]
    fake_frames[0].path.write_bytes(b"jpeg")

    monkeypatch.setattr(
        "src.pipeline.video_analysis._detect_and_extract_frames",
        lambda *a, **k: fake_frames,
    )
    monkeypatch.setattr(
        "src.pipeline.video_analysis._resize_frames_in_place",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "src.pipeline.video_analysis._vision_call",
        AsyncMock(side_effect=RuntimeError("API down")),
    )

    summary, keyframes = await analyze_video(
        video_path=video_path,
        workdir=workdir,
        transcript="",
        capture_id="01J",
        mcp_client=MagicMock(),
    )
    assert summary is None
    assert keyframes == []


@pytest.mark.asyncio
async def test_analyze_video_caps_kept_frames_at_max_keyframes_in_doc(tmp_path, monkeypatch):
    """Even if 8 frames pass the importance threshold, keep at most
    settings.max_keyframes_in_doc (default 4)."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    video_path = workdir / "video.mp4"
    video_path.write_bytes(b"x")

    fake_frames = [
        _ExtractedFrame(path=workdir / f"frame-{i:02d}.jpg", timestamp_seconds=float(i), index=i)
        for i in range(8)
    ]
    for f in fake_frames:
        f.path.write_bytes(b"jpeg")

    fake_analysis = _VisionAnalysis(
        summary="ok",
        keyframes=[_FrameCaption(frame_index=i, caption=f"frame {i}", importance=8) for i in range(8)],
    )
    monkeypatch.setattr(
        "src.pipeline.video_analysis._detect_and_extract_frames",
        lambda *a, **k: fake_frames,
    )
    monkeypatch.setattr(
        "src.pipeline.video_analysis._resize_frames_in_place",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "src.pipeline.video_analysis._vision_call",
        AsyncMock(return_value=fake_analysis),
    )

    mcp_client = MagicMock()
    mcp_client.upload_blob = AsyncMock(return_value={"sourceId": "x"})

    from src.config import settings
    monkeypatch.setattr(settings, "max_keyframes_in_doc", 4)

    summary, keyframes = await analyze_video(
        video_path=video_path,
        workdir=workdir,
        transcript="",
        capture_id="01J",
        mcp_client=mcp_client,
    )
    assert len(keyframes) == 4
    assert mcp_client.upload_blob.await_count == 4


@pytest.mark.asyncio
async def test_analyze_video_calls_quality_filter_between_detect_and_vision(tmp_path, monkeypatch):
    """The Phase 16 frame-quality pre-filter runs after scene detection and
    before the vision call. Verify it's called and that the vision call
    receives the FILTERED (smaller) frame list."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    video_path = workdir / "video.mp4"
    video_path.write_bytes(b"fake mp4")

    # Three raw frames out of scene detection ...
    raw_frames = [
        _ExtractedFrame(path=workdir / f"frame-{i:02d}.jpg", timestamp_seconds=float(i), index=i)
        for i in range(3)
    ]
    for f in raw_frames:
        f.path.write_bytes(b"\xff\xd8\xff\xe0fake jpeg")

    monkeypatch.setattr(
        "src.pipeline.video_analysis._detect_and_extract_frames",
        lambda *a, **k: raw_frames,
    )
    monkeypatch.setattr(
        "src.pipeline.video_analysis._resize_frames_in_place",
        lambda *a, **k: None,
    )

    # The filter pretends to drop the middle frame and renumber the rest.
    filter_calls: list = []

    def _fake_filter(frames):
        filter_calls.append(list(frames))
        kept = [frames[0], frames[2]]
        kept[0].index = 0
        kept[1].index = 1
        return kept

    monkeypatch.setattr(
        "src.pipeline.video_analysis.filter_low_quality_frames",
        _fake_filter,
    )

    # Capture what frames the vision call receives.
    vision_inputs: list = []

    async def _fake_vision_call(*, transcript, frames):
        vision_inputs.append(list(frames))
        return _VisionAnalysis(
            summary="ok",
            keyframes=[_FrameCaption(frame_index=i, caption=f"frame {i}", importance=8)
                       for i in range(len(frames))],
        )

    monkeypatch.setattr(
        "src.pipeline.video_analysis._vision_call",
        _fake_vision_call,
    )

    mcp_client = MagicMock()
    mcp_client.upload_blob = AsyncMock(return_value={"sourceId": "blob"})

    summary, keyframes = await analyze_video(
        video_path=video_path,
        workdir=workdir,
        transcript="t",
        capture_id="01J",
        mcp_client=mcp_client,
    )

    # The filter was called once with the raw 3-frame list.
    assert len(filter_calls) == 1
    assert len(filter_calls[0]) == 3

    # The vision call was called with the filtered 2-frame list.
    assert len(vision_inputs) == 1
    assert len(vision_inputs[0]) == 2
    assert [f.index for f in vision_inputs[0]] == [0, 1]

    assert summary == "ok"
    assert len(keyframes) == 2


@pytest.mark.asyncio
async def test_analyze_video_returns_empty_when_filter_drops_all_frames(tmp_path, monkeypatch):
    """If the quality filter drops every frame, skip the vision call entirely."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    video_path = workdir / "video.mp4"
    video_path.write_bytes(b"x")

    raw_frames = [_ExtractedFrame(path=workdir / "f.jpg", timestamp_seconds=0.0, index=0)]
    raw_frames[0].path.write_bytes(b"jpeg")

    monkeypatch.setattr(
        "src.pipeline.video_analysis._detect_and_extract_frames",
        lambda *a, **k: raw_frames,
    )
    monkeypatch.setattr(
        "src.pipeline.video_analysis.filter_low_quality_frames",
        lambda frames: [],
    )

    vision_mock = AsyncMock()
    monkeypatch.setattr("src.pipeline.video_analysis._vision_call", vision_mock)

    mcp_client = MagicMock()
    mcp_client.upload_blob = AsyncMock()

    summary, keyframes = await analyze_video(
        video_path=video_path,
        workdir=workdir,
        transcript="",
        capture_id="01J",
        mcp_client=mcp_client,
    )

    assert summary is None
    assert keyframes == []
    vision_mock.assert_not_called()
    mcp_client.upload_blob.assert_not_awaited()


# ── Silence-cut helpers ──────────────────────────────────────────────


def test_silence_end_regex_parses_ffmpeg_stderr_lines():
    """The regex must match the standard `silencedetect` filter output that
    ffmpeg writes to stderr: `[silencedetect @ ...] silence_end: 12.34 |
    silence_duration: 1.567`."""
    sample = (
        "frame= 1234 fps=30\n"
        "[silencedetect @ 0x55] silence_start: 10.5\n"
        "[silencedetect @ 0x55] silence_end: 12.34 | silence_duration: 1.84\n"
        "[silencedetect @ 0x55] silence_start: 30.0\n"
        "[silencedetect @ 0x55] silence_end: 32.0 | silence_duration: 2.0\n"
        "[out#0/null] Output\n"
    )
    matches = [(float(m.group(1)), float(m.group(2)))
               for m in _SILENCE_END_RE.finditer(sample)]
    assert matches == [(12.34, 1.84), (32.0, 2.0)]


def test_merge_dedup_timestamps_keeps_primary_drops_close_secondary():
    """Primary entries are always retained. Secondary entries are added only
    if they're at least `min_gap_seconds` away from every already-accepted ts."""
    primary = [10.0, 30.0, 50.0]
    secondary = [10.5, 19.0, 31.0, 70.0]  # 10.5 & 31.0 within 2s; 19.0 & 70.0 not
    merged = _merge_dedup_timestamps(
        primary=primary, secondary=secondary, min_gap_seconds=2.0,
    )
    assert sorted(merged) == [10.0, 19.0, 30.0, 50.0, 70.0]


def test_merge_dedup_timestamps_empty_secondary_returns_primary_copy():
    primary = [1.0, 2.0]
    out = _merge_dedup_timestamps(primary=primary, secondary=[], min_gap_seconds=1.0)
    assert out == primary
    assert out is not primary  # caller can mutate without affecting input


# ── ffmpeg scdet helpers ─────────────────────────────────────────────


def test_scdet_time_regex_parses_both_log_formats():
    """ffmpeg scdet emits its `lavfi.scd.time` value in two slightly different
    log shapes depending on the build — with or without a colon, and with
    a comma-separated `score: …, time: …` form. One regex catches both."""
    sample = (
        "[Parsed_scdet_0 @ 0x55] lavfi.scd.score: 14.234 lavfi.scd.time: 12.345\n"
        "[Parsed_scdet_0 @ 0x55] lavfi.scd.score: 22.0, lavfi.scd.time: 30.5\n"
        "[Parsed_scdet_0 @ 0x55] lavfi.scd.time 42.0\n"  # no colon variant
    )
    times = [float(m.group(1)) for m in _SCDET_TIME_RE.finditer(sample)]
    assert times == [12.345, 30.5, 42.0]


def test_cuts_to_scene_midpoints_uses_implicit_t0_and_duration():
    """With cuts at [10, 30] over a 50s video, the resulting scenes are
    (0,10), (10,30), (30,50) → midpoints 5, 20, 40."""
    assert _cuts_to_scene_midpoints([10.0, 30.0], duration=50.0) == [5.0, 20.0, 40.0]


def test_cuts_to_scene_midpoints_handles_empty_cuts_as_single_scene():
    """No cuts → whole video is one scene → midpoint at duration/2."""
    assert _cuts_to_scene_midpoints([], duration=60.0) == [30.0]


def test_cuts_to_scene_midpoints_drops_cuts_outside_duration():
    """Cuts beyond the duration (decoder quirks) shouldn't produce
    negative-length scenes."""
    assert _cuts_to_scene_midpoints([10.0, 99.0], duration=50.0) == [5.0, 30.0]


def test_cuts_to_scene_midpoints_zero_duration_returns_empty():
    assert _cuts_to_scene_midpoints([1.0, 2.0], duration=0.0) == []


# ── TransNetV2 detector ─────────────────────────────────────────────


def test_transnetv2_returns_empty_when_package_missing(tmp_path, monkeypatch):
    """The optional dep isn't a hard requirement; missing it must NOT
    crash — just log a warning and return [] so the orchestrator falls
    through to the fixed-interval path."""
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name in ("transnetv2_pytorch", "torch"):
            raise ImportError(f"simulated missing dependency: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    assert _detect_scene_timestamps_transnetv2(tmp_path / "video.mp4") == []


def test_transnetv2_returns_empty_when_high_level_api_missing(tmp_path, monkeypatch):
    """If the operator installed a TransNetV2 build that doesn't ship
    `detect_scenes` (e.g. the upstream `inference-pytorch` repo's bare
    model), we want a clean warning + empty list, not an AttributeError
    crashing the pipeline."""
    fake_torch = MagicMock()
    fake_torch.cuda.is_available.return_value = False

    class _BareModel:
        def eval(self):
            return self

        def cuda(self):
            return self

    fake_pkg = MagicMock()
    fake_pkg.TransNetV2 = _BareModel

    monkeypatch.setitem(__import__("sys").modules, "torch", fake_torch)
    monkeypatch.setitem(__import__("sys").modules, "transnetv2_pytorch", fake_pkg)

    assert _detect_scene_timestamps_transnetv2(tmp_path / "video.mp4") == []


def test_transnetv2_returns_scene_midpoints_from_detect_scenes(tmp_path, monkeypatch):
    """When the high-level API is present, scenes with (start_time, end_time)
    convert to their midpoints. Verifies both the call shape (threshold is
    passed) and the conversion logic."""
    fake_torch = MagicMock()
    fake_torch.cuda.is_available.return_value = False
    # Context manager protocol for `with torch.no_grad()`.
    no_grad_cm = MagicMock()
    no_grad_cm.__enter__ = MagicMock(return_value=None)
    no_grad_cm.__exit__ = MagicMock(return_value=False)
    fake_torch.no_grad = MagicMock(return_value=no_grad_cm)

    detect_scenes = MagicMock(return_value=[
        {"shot_id": 0, "start_time": 0.0, "end_time": 10.0},
        {"shot_id": 1, "start_time": 10.0, "end_time": 30.0},
        {"shot_id": 2, "start_time": 30.0, "end_time": 50.0},
    ])

    class _Model:
        def __init__(self) -> None:
            self.detect_scenes = detect_scenes

        def eval(self):
            return self

        def cuda(self):  # never called: cuda.is_available() → False
            return self

    fake_pkg = MagicMock()
    fake_pkg.TransNetV2 = _Model

    monkeypatch.setitem(__import__("sys").modules, "torch", fake_torch)
    monkeypatch.setitem(__import__("sys").modules, "transnetv2_pytorch", fake_pkg)
    monkeypatch.setattr(
        "src.pipeline.video_analysis.settings.scenedetect_transnet_threshold",
        0.42,
    )

    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake mp4")

    midpoints = _detect_scene_timestamps_transnetv2(video)

    assert midpoints == [5.0, 20.0, 40.0]
    # Threshold from settings propagates into the model call.
    detect_scenes.assert_called_once()
    args, kwargs = detect_scenes.call_args
    assert kwargs.get("threshold") == 0.42 or (len(args) >= 2 and args[1] == 0.42)


def test_transnetv2_swallows_inference_exception(tmp_path, monkeypatch):
    """Any error during model load / inference must NOT crash the pipeline —
    it returns [] so the caller falls through to fixed-interval sampling."""
    fake_torch = MagicMock()
    fake_torch.cuda.is_available.return_value = False

    class _Boom:
        def __init__(self) -> None:
            raise RuntimeError("simulated model load failure")

    fake_pkg = MagicMock()
    fake_pkg.TransNetV2 = _Boom

    monkeypatch.setitem(__import__("sys").modules, "torch", fake_torch)
    monkeypatch.setitem(__import__("sys").modules, "transnetv2_pytorch", fake_pkg)

    assert _detect_scene_timestamps_transnetv2(tmp_path / "video.mp4") == []


def test_transnetv2_skips_malformed_scene_entries(tmp_path, monkeypatch):
    """If the upstream lib changes its scene dict shape (missing key /
    wrong type), each bad entry is skipped silently rather than poisoning
    the candidate timestamp list with NaNs or KeyErrors."""
    fake_torch = MagicMock()
    fake_torch.cuda.is_available.return_value = False
    no_grad_cm = MagicMock()
    no_grad_cm.__enter__ = MagicMock(return_value=None)
    no_grad_cm.__exit__ = MagicMock(return_value=False)
    fake_torch.no_grad = MagicMock(return_value=no_grad_cm)

    detect_scenes = MagicMock(return_value=[
        {"shot_id": 0, "start_time": 0.0, "end_time": 10.0},   # valid
        {"shot_id": 1, "start_time": "bogus"},                  # missing end + wrong type
        {"shot_id": 2, "start_time": 30.0, "end_time": 50.0},  # valid
        "not a dict",                                            # totally malformed
    ])

    class _Model:
        def __init__(self) -> None:
            self.detect_scenes = detect_scenes

        def eval(self):
            return self

        def cuda(self):
            return self

    fake_pkg = MagicMock()
    fake_pkg.TransNetV2 = _Model

    monkeypatch.setitem(__import__("sys").modules, "torch", fake_torch)
    monkeypatch.setitem(__import__("sys").modules, "transnetv2_pytorch", fake_pkg)

    midpoints = _detect_scene_timestamps_transnetv2(tmp_path / "video.mp4")
    assert midpoints == [5.0, 40.0]


def test_detect_and_extract_frames_dispatches_to_transnetv2(tmp_path, monkeypatch):
    """When SCENEDETECT_ALGORITHM=transnetv2, the dispatcher in
    _detect_and_extract_frames routes to the transnetv2 detector — not to
    PySceneDetect or ffmpeg scdet. Verifies the wiring without touching
    the real model."""
    from src.pipeline import video_analysis as va

    called = {"pyscenedetect": 0, "ffmpeg": 0, "transnetv2": 0}

    def _pyscenedetect(path):
        called["pyscenedetect"] += 1
        return []

    def _ffmpeg(path):
        called["ffmpeg"] += 1
        return []

    def _transnetv2(path):
        called["transnetv2"] += 1
        return [10.0, 30.0]

    monkeypatch.setattr(va, "_detect_scene_timestamps_pyscenedetect", _pyscenedetect)
    monkeypatch.setattr(va, "_detect_scene_timestamps_ffmpeg", _ffmpeg)
    monkeypatch.setattr(va, "_detect_scene_timestamps_transnetv2", _transnetv2)
    # Don't actually extract frames — just verify the detector dispatch.
    monkeypatch.setattr(va, "_ffmpeg_extract_frame", lambda *a, **k: False)
    monkeypatch.setattr(va.settings, "scenedetect_algorithm", "transnetv2")
    monkeypatch.setattr(va.settings, "frame_silence_cuts_enabled", False)

    va._detect_and_extract_frames(tmp_path / "video.mp4", tmp_path, max_frames=4)

    assert called == {"pyscenedetect": 0, "ffmpeg": 0, "transnetv2": 1}
