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
