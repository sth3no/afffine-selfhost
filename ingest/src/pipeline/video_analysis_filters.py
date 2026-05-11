"""Phase 16 — frame-quality pre-filter.

Cheap heuristics that drop obviously-useless frames before the (expensive)
Sonnet 4.6 vision call. Three filters, applied in cheap-to-expensive order:

  1. Blackness  — mean grayscale pixel value below threshold (intro fades,
                  transition frames, hidden cuts).
  2. Entropy    — Shannon entropy of grayscale histogram below threshold
                  (blank slides, solid-color cards).
  3. pHash dedup — perceptual hash of the frame within hamming distance
                   of an already-kept frame (same title-card flashing twice).

Public entry point: `filter_low_quality_frames(frames)`. Returns the surviving
frames in timestamp order with `.index` renumbered from 0.

Mutates `frame.index` in place (the `_ExtractedFrame` dataclass is not frozen).
This avoids importing `_ExtractedFrame` from `video_analysis` at module load
time and breaking the import cycle.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.pipeline.video_analysis import _ExtractedFrame

from src.config import settings

log = logging.getLogger(__name__)


# ── Filter: blackness ────────────────────────────────────────────────


def _is_too_black(frame_path: Path) -> bool:
    """True when the frame's mean grayscale pixel value is below the
    blackness threshold. Used to drop intro-fade / transition frames."""
    import numpy as np
    from PIL import Image

    try:
        with Image.open(frame_path) as im:
            gray = im.convert("L")
            arr = np.asarray(gray, dtype=np.uint8)
    except Exception as e:  # noqa: BLE001 — best-effort, never raise
        log.warning("blackness check failed for %s: %s; keeping frame", frame_path, e)
        return False

    mean_val = float(arr.mean()) if arr.size else 0.0
    return mean_val < settings.frame_blackness_threshold


# ── Filter: entropy ──────────────────────────────────────────────────


def _is_too_low_entropy(frame_path: Path) -> bool:
    """True when the Shannon entropy of the frame's grayscale histogram
    is below the configured threshold (bits). Drops blank slides,
    solid-color title cards, single-tone transitions."""
    import numpy as np
    from PIL import Image

    try:
        with Image.open(frame_path) as im:
            gray = im.convert("L")
            arr = np.asarray(gray, dtype=np.uint8)
    except Exception as e:  # noqa: BLE001
        log.warning("entropy check failed for %s: %s; keeping frame", frame_path, e)
        return False

    if arr.size == 0:
        return True

    # 256-bin histogram of 0-255 grayscale values, normalized to probability.
    counts = np.bincount(arr.ravel(), minlength=256).astype(np.float64)
    probs = counts / counts.sum()
    # Shannon entropy in bits. Replace 0-probability bins with 1 so log2(1)=0
    # contributes nothing (avoids log(0) = -inf NaN).
    nonzero = probs > 0
    entropy_bits = float(-(probs[nonzero] * np.log2(probs[nonzero])).sum())
    return entropy_bits < settings.frame_entropy_threshold


# ── Filter: perceptual-hash dedup ────────────────────────────────────


def _compute_phash(frame_path: Path):
    """Compute a 64-bit perceptual hash for the frame. Returns the
    `imagehash.ImageHash` object (which supports `-` for hamming distance)
    or None on any failure (caller treats None as "can't dedup, keep")."""
    try:
        import imagehash
        from PIL import Image
    except ImportError as e:
        log.warning("imagehash/Pillow unavailable: %s; skipping dedup", e)
        return None

    try:
        with Image.open(frame_path) as im:
            return imagehash.phash(im)
    except Exception as e:  # noqa: BLE001
        log.warning("phash compute failed for %s: %s; keeping frame", frame_path, e)
        return None


# ── Public entry point ───────────────────────────────────────────────


def filter_low_quality_frames(
    frames: "list[_ExtractedFrame]",
) -> "list[_ExtractedFrame]":
    """Drop frames that are too dark, too uniform, or near-duplicates of an
    already-kept frame. Returns the survivors in timestamp order, with
    `.index` renumbered from 0 (since the vision call uses these indices
    as identifiers, gaps would confuse the LLM).

    Filter order is cheap → expensive: blackness, then entropy, then pHash.
    The earlier filters short-circuit so we don't pay for pHash on a frame
    that's already going to be dropped."""
    if not frames:
        return []

    ordered = sorted(frames, key=lambda f: f.timestamp_seconds)
    kept: list = []  # `list[_ExtractedFrame]` at runtime
    kept_hashes: list = []  # one entry per kept frame; None means "no hash, skip dedup compare"

    for f in ordered:
        if _is_too_black(f.path):
            log.info("frame-filter: drop t=%.2fs (too dark)", f.timestamp_seconds)
            continue
        if _is_too_low_entropy(f.path):
            log.info("frame-filter: drop t=%.2fs (low entropy)", f.timestamp_seconds)
            continue
        h = _compute_phash(f.path)
        is_dup = False
        if h is not None:
            for kh in kept_hashes:
                if kh is not None and (h - kh) <= settings.frame_dedup_hamming_distance:
                    is_dup = True
                    break
        if is_dup:
            log.info("frame-filter: drop t=%.2fs (near-duplicate)", f.timestamp_seconds)
            continue
        kept.append(f)
        kept_hashes.append(h)

    # Renumber sequentially (in place — the dataclass is not frozen).
    for i, f in enumerate(kept):
        f.index = i

    log.info(
        "frame-filter: kept %d / %d frames after blackness+entropy+phash pre-filter",
        len(kept), len(frames),
    )
    return kept
