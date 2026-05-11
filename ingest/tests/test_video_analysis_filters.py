"""Tests for Phase 16 frame-quality pre-filter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
from PIL import Image


# Local re-import of _ExtractedFrame's shape so we don't need to import it
# from video_analysis (avoids triggering scenedetect/anthropic on import
# during these pure-Python tests). The filter module accepts duck-typed
# objects with .path, .timestamp_seconds, .index attributes.
@dataclass
class _Frame:
    path: Path
    timestamp_seconds: float
    index: int


def _solid_color_jpeg(path: Path, color: tuple[int, int, int], size: tuple[int, int] = (100, 100)) -> Path:
    """Write a solid-color JPEG to `path` and return it."""
    Image.new("RGB", size, color=color).save(path, "JPEG", quality=85)
    return path


def _noise_jpeg(path: Path, size: tuple[int, int] = (100, 100), seed: int = 0) -> Path:
    """Write a random-noise RGB JPEG to `path`. High Shannon entropy."""
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 256, size=(size[1], size[0], 3), dtype=np.uint8)
    Image.fromarray(arr).save(path, "JPEG", quality=85)
    return path


# ── Blackness filter ─────────────────────────────────────────────────


def test_is_too_black_drops_black_frame(tmp_path):
    from src.pipeline.video_analysis_filters import _is_too_black
    p = _solid_color_jpeg(tmp_path / "black.jpg", (0, 0, 0))
    assert _is_too_black(p) is True


def test_is_too_black_keeps_bright_frame(tmp_path):
    from src.pipeline.video_analysis_filters import _is_too_black
    p = _solid_color_jpeg(tmp_path / "white.jpg", (255, 255, 255))
    assert _is_too_black(p) is False


def test_is_too_black_keeps_mid_gray_frame(tmp_path):
    """A 128-gray frame is well above the default blackness threshold (20)."""
    from src.pipeline.video_analysis_filters import _is_too_black
    p = _solid_color_jpeg(tmp_path / "gray.jpg", (128, 128, 128))
    assert _is_too_black(p) is False


# ── Entropy filter ───────────────────────────────────────────────────


def test_is_too_low_entropy_drops_uniform_gray(tmp_path):
    """A solid-color frame has near-zero entropy → drop."""
    from src.pipeline.video_analysis_filters import _is_too_low_entropy
    p = _solid_color_jpeg(tmp_path / "gray.jpg", (128, 128, 128))
    assert _is_too_low_entropy(p) is True


def test_is_too_low_entropy_keeps_noisy_frame(tmp_path):
    """Random RGB noise has high entropy → keep."""
    from src.pipeline.video_analysis_filters import _is_too_low_entropy
    p = _noise_jpeg(tmp_path / "noise.jpg")
    assert _is_too_low_entropy(p) is False


# ── pHash dedup ──────────────────────────────────────────────────────


def test_compute_phash_returns_same_hash_for_identical_images(tmp_path):
    """Pixel-identical images produce identical pHashes (hamming distance 0)."""
    from src.pipeline.video_analysis_filters import _compute_phash

    p1 = _solid_color_jpeg(tmp_path / "a.jpg", (10, 100, 200))
    p2 = _solid_color_jpeg(tmp_path / "b.jpg", (10, 100, 200))
    h1 = _compute_phash(p1)
    h2 = _compute_phash(p2)
    assert h1 is not None and h2 is not None
    assert (h1 - h2) == 0


def test_compute_phash_returns_distant_hash_for_different_images(tmp_path):
    """Two very different images have a large hamming distance (well above the dedup threshold)."""
    from src.pipeline.video_analysis_filters import _compute_phash

    p1 = _solid_color_jpeg(tmp_path / "black.jpg", (0, 0, 0))
    p2 = _noise_jpeg(tmp_path / "noise.jpg")
    h1 = _compute_phash(p1)
    h2 = _compute_phash(p2)
    assert h1 is not None and h2 is not None
    assert (h1 - h2) > 5  # well above the default dedup threshold


# ── Public entry point ───────────────────────────────────────────────


def test_filter_drops_black_and_dedups_and_renumbers(tmp_path):
    """Integration: 4 input frames — black (drop), unique-A (keep), unique-A-again (drop dup),
    unique-B (keep). Result: 2 frames, renumbered 0 and 1."""
    from src.pipeline.video_analysis_filters import filter_low_quality_frames

    p_black = _solid_color_jpeg(tmp_path / "f0.jpg", (0, 0, 0))
    p_a1 = _noise_jpeg(tmp_path / "f1.jpg", seed=1)
    p_a2 = _noise_jpeg(tmp_path / "f2.jpg", seed=1)   # bit-identical → dup of f1
    p_b = _noise_jpeg(tmp_path / "f3.jpg", seed=42)   # different seed → distinct content

    frames = [
        _Frame(path=p_black, timestamp_seconds=0.0, index=0),
        _Frame(path=p_a1, timestamp_seconds=5.0, index=1),
        _Frame(path=p_a2, timestamp_seconds=10.0, index=2),
        _Frame(path=p_b, timestamp_seconds=15.0, index=3),
    ]

    kept = filter_low_quality_frames(frames)

    # The black frame is dropped (blackness); the duplicate of f1 is dropped (phash).
    # Two frames remain, renumbered 0 and 1 in timestamp order.
    assert len(kept) == 2
    assert kept[0].timestamp_seconds == 5.0
    assert kept[0].index == 0
    assert kept[1].timestamp_seconds == 15.0
    assert kept[1].index == 1


def test_filter_keeps_first_of_each_dup_group_by_timestamp(tmp_path):
    """When two duplicates appear, the EARLIER timestamp wins."""
    from src.pipeline.video_analysis_filters import filter_low_quality_frames

    p1 = _noise_jpeg(tmp_path / "f1.jpg", seed=7)
    p2 = _noise_jpeg(tmp_path / "f2.jpg", seed=7)  # bit-identical

    frames = [
        _Frame(path=p2, timestamp_seconds=20.0, index=1),  # later timestamp, given to filter first in list order
        _Frame(path=p1, timestamp_seconds=5.0, index=0),
    ]

    kept = filter_low_quality_frames(frames)
    # The filter sorts by timestamp before deduping, so the t=5.0 frame survives.
    assert len(kept) == 1
    assert kept[0].timestamp_seconds == 5.0


def test_filter_returns_empty_for_empty_input(tmp_path):
    from src.pipeline.video_analysis_filters import filter_low_quality_frames
    assert filter_low_quality_frames([]) == []
