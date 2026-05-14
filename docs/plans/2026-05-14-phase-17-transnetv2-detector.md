# Phase 17 — TransNetV2 as a fourth `SCENEDETECT_ALGORITHM` value

**Date:** 2026-05-14
**Builds on:**
- [`2026-05-14-video-analysis-ml-acceleration-roadmap.md`](2026-05-14-video-analysis-ml-acceleration-roadmap.md) — this is Stage A of that roadmap.
- [`2026-05-08-phase-13-video-frame-analysis.md`](2026-05-08-phase-13-video-frame-analysis.md) — the original detector pipeline.

---

## 1. Goal

Add TransNetV2 (Souček & Lokoč, ACM Multimedia 2024 — CNN+LSTM shot
boundary detector) as an opt-in fourth value for `SCENEDETECT_ALGORITHM`,
alongside the existing `adaptive` / `content` / `ffmpeg_scdet`. TransNetV2
is materially better than any of those at slow dissolves and fades — the
exact cases where the current `adaptive` path needs its half-sensitivity
retry and `ffmpeg_scdet` misses entirely.

**Non-goals:**
- No replacement of the existing algorithms. Operator picks.
- No hybrid path (PySceneDetect for fast cuts + TransNetV2 only on
  dissolve-suspect regions). Future work, not this phase.
- No model code in the repo. We integrate against the maintained
  `transnetv2-pytorch` PyPI package's high-level
  `detect_scenes(path, threshold=...)` API.
- No new Docker layer. `torch` + `transnetv2-pytorch` stay optional
  imports — selfhost users who don't want the GB of dependencies don't
  pay the size cost.

## 2. Scope

| File | Change |
|---|---|
| `ingest/src/config.py` | Add `scenedetect_transnet_threshold: float = 0.5` and `scenedetect_transnet_weights: str = ""`; document `"transnetv2"` as a valid `scenedetect_algorithm` value. |
| `ingest/src/pipeline/video_analysis.py` | New `_detect_scene_timestamps_transnetv2(video_path) -> list[float]`. Wire it into the algorithm dispatcher in `_detect_and_extract_frames`. |
| `ingest/tests/test_video_analysis.py` | Six new tests covering: missing-package fallback, missing-API fallback, midpoint conversion with mocked `detect_scenes`, exception swallowing on model load failure, malformed-scene-entry resilience, dispatcher routing. |

No migrations. No new Docker layers. No README copy in this phase (Stage B
will demand a "self-hosting the vision model" section; not relevant yet).

## 3. Design notes

### 3.1 Optional-import pattern

Mirrors PySceneDetect's existing handling — the import lives inside the
function body, `ImportError` returns `[]` with a warning, and the
orchestrator falls through to the fixed-interval sampling path. The
caller _never_ crashes.

### 3.2 Why `detect_scenes` and not the low-level model

The upstream `soCzech/TransNetV2` repo ships only the raw model forward
pass + a TensorFlow→PyTorch weight conversion script — running it would
require us to write our own 100-frame chunked decoder, 48×27 resize,
sigmoid threshold, and scene-conversion pipeline. The maintained
`transnetv2-pytorch` PyPI fork wraps all of that behind one
`detect_scenes(video_path, threshold=0.5)` call returning
`[{shot_id, start_time, end_time}, ...]`.

We integrate against the PyPI API and use `hasattr(model,
"detect_scenes")` to detect operators who installed the bare upstream
build — those get a clean warning + fallback rather than an
`AttributeError` mid-pipeline.

### 3.3 Auto-device selection

`if torch.cuda.is_available(): model = model.cuda()`. No new config
knob — the operator's hardware already determines this. CPU inference
is tractable for short clips (~50-100 fps on a modern x86); GPU
inference is ~10-20× faster but not required.

### 3.4 Optional weights path

`scenedetect_transnet_weights` (default `""`) lets operators who
vendor the `transnetv2-pytorch-weights.pth` file at a known location
load it explicitly. Empty string = trust the installed package to
resolve its own weights (auto-download or bundled). This covers both
the read-only-mount Kubernetes use-case and the easy `pip install`
single-user case.

### 3.5 Output shape

Scenes have `(start_time, end_time)` in seconds. We compute the
midpoint `(start+end)/2` per scene — same shape every other detector
function in this module returns (`list[float]` of scene midpoints).
The downstream pipeline (silencedetect merge, thumbnail-window pick,
parallel ffmpeg extract, quality filter, vision call) needs no change.

## 4. Test strategy

All tests use mocks. None of them load a real model or touch a real
video file. The mock setup pattern is:

```python
fake_torch = MagicMock(); fake_torch.cuda.is_available.return_value = False
fake_pkg = MagicMock(); fake_pkg.TransNetV2 = _Model
monkeypatch.setitem(sys.modules, "torch", fake_torch)
monkeypatch.setitem(sys.modules, "transnetv2_pytorch", fake_pkg)
```

This injects fake `torch` and `transnetv2_pytorch` modules into
`sys.modules` so the function-local `import` inside
`_detect_scene_timestamps_transnetv2` resolves to the fake. No real
GPU, no real model, no real weights download in CI.

Coverage:
1. `test_transnetv2_returns_empty_when_package_missing` — `ImportError`
   path. Patches `builtins.__import__` to raise on the target modules.
2. `test_transnetv2_returns_empty_when_high_level_api_missing` —
   operator installed a bare-model variant; `hasattr` check kicks in,
   warning + empty list.
3. `test_transnetv2_returns_scene_midpoints_from_detect_scenes` —
   happy path with three scenes; verifies midpoint computation AND
   that the configured threshold is passed to `detect_scenes`.
4. `test_transnetv2_swallows_inference_exception` — model constructor
   raises; we return `[]` instead of crashing.
5. `test_transnetv2_skips_malformed_scene_entries` — if the upstream
   library changes its dict shape, bad entries are skipped silently
   rather than poisoning the candidate list with `KeyError` / `NaN`.
6. `test_detect_and_extract_frames_dispatches_to_transnetv2` — when
   `SCENEDETECT_ALGORITHM=transnetv2`, the in-function dispatcher
   routes to the new detector (not PySceneDetect, not scdet).

## 5. Risks and mitigations

| Risk | Mitigation |
|---|---|
| `transnetv2-pytorch` PyPI package API drift (`detect_scenes` rename or signature change) | `hasattr` guard, mock-driven tests caught at import time, all paths fall back to `[]` → fixed-interval frames. Operator gets a warning, not a crash. |
| Operator installs upstream `inference-pytorch` (no high-level API) | Same `hasattr` guard. Clear warning text tells them to use the PyPI package. |
| CPU inference is too slow on long videos | Already cached behind `torch.cuda.is_available()`. CPU path is a fallback; operators with long-form video are nudged toward GPU by docs (this roadmap section). |
| `torch` is a 500 MB+ install | Optional. Selfhost users who don't want it don't install it — the default algorithm is still `adaptive`, which works without `torch`. |
| Model weights file not present in some packaging path | `scenedetect_transnet_weights` env var lets operator pin a local `.pth`. Empty default trusts the package. |

## 6. Verification before merge

- `pytest tests/test_video_analysis.py -v` → all 21 tests pass.
- `python -c "from src.pipeline.video_analysis import _detect_scene_timestamps_transnetv2"` → no import-time failure even when `transnetv2-pytorch` is absent (graceful: the import is function-local).
- Default behavior unchanged: `SCENEDETECT_ALGORITHM` still defaults to `adaptive`; no existing tests touched.

## 7. Follow-ups (not in this phase)

- **Stage B of the parent roadmap** (pluggable VLM provider) — separate phase.
- A `docker-compose.gpu.yml` overlay with `torch` + `transnetv2-pytorch`
  pre-installed for operators who want one-command GPU setup. Probably
  worth doing alongside Stage B, not on its own.
- Hybrid detector: PySceneDetect / scdet for fast cuts +
  TransNetV2 _only_ on regions where the fast detector's confidence is
  low. Would give us TransNetV2's dissolve accuracy at near-scdet
  speed. Defer until we have telemetry showing TransNetV2 latency is
  actually a problem in practice.
