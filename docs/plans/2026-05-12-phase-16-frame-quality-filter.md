# Phase 16 — Frame-quality pre-filter

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drop obviously uninformative video frames (mostly-black, near-duplicate, low-entropy) *before* the Sonnet 4.6 vision call runs. Cuts vision-token cost by ~30–40 % on typical YouTube videos and gives the per-frame importance signal a cleaner input set.

**Architecture:** A new pure-Python module `video_analysis_filters` exposes `filter_low_quality_frames(frames)` that runs three cheap heuristics — mean-brightness blackness, Shannon-entropy of grayscale histogram, and perceptual-hash (`imagehash.phash`) deduplication — and returns the surviving frames re-indexed from 0. The orchestrator wires it in between `_detect_and_extract_frames()` and `_resize_frames_in_place()` inside `analyze_video()`.

**Tech Stack:**
- `numpy` + `Pillow` (both already in deps) for blackness and entropy
- `imagehash>=4.3` (new dep, pure-Python, depends on Pillow + numpy) for perceptual hash
- `pytest` for filter unit tests + an integration assertion against `analyze_video()`

**Roadmap reference:** [`2026-05-12-video-frame-analysis-roadmap.md`](2026-05-12-video-frame-analysis-roadmap.md) Tier 5
**Macro plan:** [`2026-05-12-video-frame-analysis-macro-plan.md`](2026-05-12-video-frame-analysis-macro-plan.md) Phase 16

**End-of-phase test count:** existing 402 passed + ~12 new tests.

---

## File structure

| File | Action | Responsibility |
|---|---|---|
| `ingest/pyproject.toml` | Modify | Add `imagehash>=4.3` to `[project] dependencies`. |
| `ingest/src/config.py` | Modify | Three new settings: `frame_blackness_threshold` (float, 0–255 mean), `frame_dedup_hamming_distance` (int, 0–64 pHash distance), `frame_entropy_threshold` (float, bits 0–8). |
| `ingest/src/pipeline/video_analysis_filters.py` | Create | `filter_low_quality_frames()` public entry + three private filter functions: `_is_too_black`, `_is_too_low_entropy`, `_compute_phash`. Uses `TYPE_CHECKING` for the `_ExtractedFrame` annotation to avoid an import cycle; mutates the frame's `index` in place rather than constructing new instances. |
| `ingest/src/pipeline/video_analysis.py` | Modify | Inside `analyze_video()`, between `_detect_and_extract_frames()` and `_resize_frames_in_place()`, call `filter_low_quality_frames(frames)`; early-return `(None, [])` if the filter empties the list. |
| `ingest/tests/test_video_analysis_filters.py` | Create | 6 new tests for the filter module (one per filter + dedup + public-entry + renumber). |
| `ingest/tests/test_video_analysis.py` | Modify | 1 new test asserting `analyze_video()` calls the filter and shrinks the frame list before the vision call. |

**Why a mutation-based renumber instead of new-instance construction:** `_ExtractedFrame` is defined in `video_analysis.py`. If `video_analysis_filters.py` constructed new instances it would need to import the type at runtime, but `video_analysis.py` also imports `filter_low_quality_frames` — a cycle. Mutating `index` in place lets the filter module keep `_ExtractedFrame` as a `TYPE_CHECKING`-only reference, no runtime import needed. The dataclass is already mutable (not frozen), so this is well-defined.

---

## Task 1: Add dependency and config settings

**Files:**
- Modify: `ingest/pyproject.toml`
- Modify: `ingest/src/config.py`

- [ ] **Step 1.1: Add `imagehash` to dependencies**

Open `ingest/pyproject.toml`. Inside `[project] dependencies` (the existing array, currently ending with `"markdown-it-py>=3.0",`), append `imagehash>=4.3`:

```toml
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "asyncpg>=0.29",
    "pydantic>=2.7",
    "pydantic-settings>=2.5",
    "httpx>=0.27",
    "pyyaml>=6.0",
    "python-ulid>=2.4",
    "yt-dlp>=2025.1.0",
    "bgutil-ytdlp-pot-provider>=1.0",
    "youtube-transcript-api>=1.0.0",
    "markitdown>=0.0.1",
    "openai>=1.40",
    "anthropic>=0.40",
    "numpy>=1.26",
    "scenedetect[opencv]>=0.6.4",
    "Pillow>=10.0",
    "markdown-it-py>=3.0",
    "imagehash>=4.3",
]
```

- [ ] **Step 1.2: Install the new dependency locally so the tests can import it**

```bash
cd ingest && pip install "imagehash>=4.3"
```

Expected: install completes successfully. (CI rebuilds will pick it up from `pyproject.toml`.)

- [ ] **Step 1.3: Add the three filter settings to `Settings`**

Open `ingest/src/config.py`. After the existing video-frame-analysis settings block (currently ending with `scenedetect_threshold: float = 27.0`), append:

```python
    # Phase 16 — Frame-quality pre-filter (runs BETWEEN scene detect and vision call).
    # Defaults chosen to be conservative: drop only frames that are
    # obviously useless. Tune via env vars if the filter is over- or
    # under-aggressive on a particular video corpus.
    frame_blackness_threshold: float = 20.0   # mean grayscale pixel value 0-255; below = "too dark"
    frame_dedup_hamming_distance: int = 5     # imagehash pHash distance 0-64; below-or-equal = "duplicate"
    frame_entropy_threshold: float = 4.0      # Shannon entropy of grayscale histogram, bits 0-8; below = "too uniform"
```

- [ ] **Step 1.4: Verify the settings load**

```bash
cd ingest && python -c "from src.config import settings; print(settings.frame_blackness_threshold, settings.frame_dedup_hamming_distance, settings.frame_entropy_threshold)"
```

Expected: `20.0 5 4.0` on stdout.

- [ ] **Step 1.5: Commit**

```bash
git add ingest/pyproject.toml ingest/src/config.py
git commit -m "feat(ingest): add imagehash dep + frame-quality filter settings"
```

---

## Task 2: Implement `video_analysis_filters.py`

Test-driven per filter, all in one module + one commit at the end of the task.

**Files:**
- Create: `ingest/src/pipeline/video_analysis_filters.py`
- Create: `ingest/tests/test_video_analysis_filters.py`

### Sub-task 2A: Blackness filter

- [ ] **Step 2A.1: Write the failing tests**

Create `ingest/tests/test_video_analysis_filters.py` with:

```python
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
```

- [ ] **Step 2A.2: Run the tests to verify failure**

```bash
cd ingest && python -m pytest tests/test_video_analysis_filters.py::test_is_too_black_drops_black_frame -v
```

Expected: FAIL with `ImportError: cannot import name '_is_too_black'`.

- [ ] **Step 2A.3: Create the filter module with `_is_too_black`**

Create `ingest/src/pipeline/video_analysis_filters.py`:

```python
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
```

- [ ] **Step 2A.4: Run blackness tests to verify they pass**

```bash
cd ingest && python -m pytest tests/test_video_analysis_filters.py -k is_too_black -v
```

Expected: 3 PASS.

### Sub-task 2B: Entropy filter

- [ ] **Step 2B.1: Append failing tests**

Append to `ingest/tests/test_video_analysis_filters.py`:

```python
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
```

- [ ] **Step 2B.2: Run, verify failure**

```bash
cd ingest && python -m pytest tests/test_video_analysis_filters.py::test_is_too_low_entropy_drops_uniform_gray -v
```

Expected: FAIL with `ImportError`.

- [ ] **Step 2B.3: Append `_is_too_low_entropy` to the filter module**

Append to `ingest/src/pipeline/video_analysis_filters.py`:

```python
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
```

- [ ] **Step 2B.4: Run, verify pass**

```bash
cd ingest && python -m pytest tests/test_video_analysis_filters.py -k entropy -v
```

Expected: 2 PASS.

### Sub-task 2C: pHash dedup + public entry point

- [ ] **Step 2C.1: Append failing tests for `_compute_phash` and the public `filter_low_quality_frames`**

Append to `ingest/tests/test_video_analysis_filters.py`:

```python
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
```

- [ ] **Step 2C.2: Run, verify failure**

```bash
cd ingest && python -m pytest tests/test_video_analysis_filters.py::test_filter_drops_black_and_dedups_and_renumbers -v
```

Expected: FAIL with `ImportError: cannot import name 'filter_low_quality_frames'` (or `_compute_phash`).

- [ ] **Step 2C.3: Append `_compute_phash` and `filter_low_quality_frames` to the filter module**

Append to `ingest/src/pipeline/video_analysis_filters.py`:

```python
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
```

- [ ] **Step 2C.4: Run all filter tests**

```bash
cd ingest && python -m pytest tests/test_video_analysis_filters.py -v 2>&1 | tail -20
```

Expected: 10 PASS (3 blackness + 2 entropy + 2 phash + 3 public-entry-point).

- [ ] **Step 2C.5: Commit Task 2**

```bash
git add ingest/src/pipeline/video_analysis_filters.py ingest/tests/test_video_analysis_filters.py
git commit -m "feat(ingest): video_analysis_filters with blackness/entropy/phash pre-filter"
```

---

## Task 3: Wire `filter_low_quality_frames` into `analyze_video()`

**Files:**
- Modify: `ingest/src/pipeline/video_analysis.py`
- Modify: `ingest/tests/test_video_analysis.py`

- [ ] **Step 3.1: Write the failing integration test**

Append to `ingest/tests/test_video_analysis.py` (after the existing happy-path test):

```python
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
```

- [ ] **Step 3.2: Run to verify failure**

```bash
cd ingest && python -m pytest tests/test_video_analysis.py::test_analyze_video_calls_quality_filter_between_detect_and_vision -v
```

Expected: FAIL — either `AttributeError: module 'src.pipeline.video_analysis' has no attribute 'filter_low_quality_frames'` (monkeypatch can't find the symbol), or the vision call still receives all 3 frames.

- [ ] **Step 3.3: Wire the filter into `analyze_video()`**

Open `ingest/src/pipeline/video_analysis.py`. Add the import near the top, after the existing `from src.config import settings` line:

```python
from src.config import settings
from src.pipeline.video_analysis_filters import filter_low_quality_frames
```

Inside `analyze_video()`, locate the section just after `_detect_and_extract_frames` returns. The current shape is:

```python
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

    # Stage 3: resize via Pillow (still sync; fold into the to_thread).
```

Insert a new stage between the "no scenes detected" early-return and the resize step:

```python
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
```

- [ ] **Step 3.4: Run to verify pass**

```bash
cd ingest && python -m pytest tests/test_video_analysis.py -v 2>&1 | tail -15
```

Expected: all existing tests still pass + 2 new tests pass.

- [ ] **Step 3.5: Run the full ingest suite to confirm no regressions**

```bash
cd ingest && python -m pytest 2>&1 | tail -5
```

Expected: ~414 passed, 9 skipped (was 402; +12 new tests).

- [ ] **Step 3.6: Commit**

```bash
git add ingest/src/pipeline/video_analysis.py ingest/tests/test_video_analysis.py
git commit -m "feat(ingest): wire frame-quality pre-filter into analyze_video()"
```

---

## Task 4: Update macro plan + final verify + PR

**Files:**
- Modify: `docs/plans/2026-05-12-video-frame-analysis-macro-plan.md`

- [ ] **Step 4.1: Mark Phase 16 as done in the macro plan**

Open `docs/plans/2026-05-12-video-frame-analysis-macro-plan.md`. Find the Phase 16 section. After its `**Effort:** ~3-5 hours. Single PR.` line, append:

```markdown
**Status:** ✅ Shipped (commit `<SHA>` — fill in after the previous commits).
**Detailed plan:** [`2026-05-12-phase-16-frame-quality-filter.md`](2026-05-12-phase-16-frame-quality-filter.md)
```

Get the SHA from `git log --oneline -1` after Task 3's commit.

- [ ] **Step 4.2: Run the full ingest test suite end-to-end one more time**

```bash
cd ingest && python -m pytest 2>&1 | tail -5
```

Expected: ~414 passed, 9 skipped.

- [ ] **Step 4.3: Final commit + push**

```bash
git add docs/plans/2026-05-12-video-frame-analysis-macro-plan.md
git commit -m "docs(plans): mark Phase 16 (frame-quality filter) shipped"
git push
```

- [ ] **Step 4.4: Open the PR**

```bash
gh pr create --title "feat(ingest): Phase 16 — frame-quality pre-filter (Tier 5)" --body "$(cat <<'EOF'
## Summary

Tier 5 of the video frame analysis roadmap. Drops obviously-useless video frames (mostly-black, near-duplicate, low-entropy) BEFORE the Sonnet 4.6 vision call runs. Reduces vision-token cost ~30-40% on typical YouTube videos and gives the per-frame importance signal a cleaner input set.

## Changes

### New module: `video_analysis_filters.py`

Three pure-Python heuristics, applied in cheap-to-expensive order:
- **Blackness** — `np.mean(grayscale)` < `frame_blackness_threshold` (default 20 / 255). Drops intro fades, transitions.
- **Entropy** — Shannon entropy of grayscale histogram (bits) < `frame_entropy_threshold` (default 4). Drops blank slides, solid title cards.
- **pHash dedup** — `imagehash.phash` hamming distance ≤ `frame_dedup_hamming_distance` (default 5) against any kept frame. Drops same-title-card-flashing-twice cases. Earliest-timestamp wins.

Public entry: `filter_low_quality_frames(frames)`. Sorts by timestamp, applies the three filters in order with short-circuit, mutates `.index` in place to renumber 0..M-1 (vision call uses these as identifiers — gaps would confuse the LLM).

### Wired into `analyze_video()`

New "Stage 2.5" between `_detect_and_extract_frames()` and `_resize_frames_in_place()`. Runs in a thread (`asyncio.to_thread`) since the filters are sync numpy/Pillow/imagehash. Best-effort — a filter exception is logged and the unfiltered frames continue downstream.

If the filter drops EVERY frame, `analyze_video()` returns `(None, [])` early — same shape as the existing "no scenes detected" path.

### Settings

Three new env-tunable fields on `Settings`:
- `frame_blackness_threshold: float = 20.0`
- `frame_dedup_hamming_distance: int = 5`
- `frame_entropy_threshold: float = 4.0`

### Dependency

Adds `imagehash>=4.3` (pure-Python; transitive deps: Pillow + numpy, both already present).

## Test plan

- [x] **~414 passed, 9 skipped** (was 402; +12 new tests):
  - `test_is_too_black_drops_black_frame`
  - `test_is_too_black_keeps_bright_frame`
  - `test_is_too_black_keeps_mid_gray_frame`
  - `test_is_too_low_entropy_drops_uniform_gray`
  - `test_is_too_low_entropy_keeps_noisy_frame`
  - `test_compute_phash_returns_same_hash_for_identical_images`
  - `test_compute_phash_returns_distant_hash_for_different_images`
  - `test_filter_drops_black_and_dedups_and_renumbers`
  - `test_filter_keeps_first_of_each_dup_group_by_timestamp`
  - `test_filter_returns_empty_for_empty_input`
  - `test_analyze_video_calls_quality_filter_between_detect_and_vision`
  - `test_analyze_video_returns_empty_when_filter_drops_all_frames`
  - (count up to whatever lands — the per-filter assertions are split across multiple tests)
- [ ] **After merge — operator smoke**:
  - `git pull`, `docker compose build --no-cache ingest`, `docker compose up -d --force-recreate ingest`
  - (No migration this phase — just a code+deps change.)
  - Capture a new YouTube video that has an obvious intro-fade. In the ingest logs grep for `frame-filter: drop ... (too dark)` to confirm the filter is firing. Cross-check the rendered doc still has reasonable inline `affine:image` blocks or `## Keyframes` appendix.

## Related

- Roadmap: [`docs/plans/2026-05-12-video-frame-analysis-roadmap.md`](docs/plans/2026-05-12-video-frame-analysis-roadmap.md) Tier 5
- Macro plan: [`docs/plans/2026-05-12-video-frame-analysis-macro-plan.md`](docs/plans/2026-05-12-video-frame-analysis-macro-plan.md) Phase 16
- Detailed plan: [`docs/plans/2026-05-12-phase-16-frame-quality-filter.md`](docs/plans/2026-05-12-phase-16-frame-quality-filter.md)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Verification checklist (engineer self-check before declaring done)

- [ ] `imagehash>=4.3` is in `pyproject.toml` and installs cleanly.
- [ ] Three new settings appear on `Settings` and load with their default values.
- [ ] `_is_too_black` drops a (0,0,0) JPEG and keeps a (255,255,255) JPEG.
- [ ] `_is_too_low_entropy` drops a solid-color JPEG and keeps a numpy-noise JPEG.
- [ ] `_compute_phash` returns identical hashes for pixel-identical JPEGs (distance 0) and dissimilar hashes for visually-different ones (distance > 5).
- [ ] `filter_low_quality_frames` drops black + dedup'd duplicates and renumbers survivors from 0.
- [ ] When two duplicates differ in timestamp, the earlier timestamp survives.
- [ ] `analyze_video()` calls the filter exactly once, between scene detect and vision call, and the vision call receives the filtered (shorter) list.
- [ ] When the filter empties the frame list, `analyze_video()` returns `(None, [])` WITHOUT calling the vision API or `upload_blob`.
- [ ] Full ingest test suite passes (~410 passed, 9 skipped).
- [ ] PR description includes the operator smoke checklist (build/redeploy/log-grep).
