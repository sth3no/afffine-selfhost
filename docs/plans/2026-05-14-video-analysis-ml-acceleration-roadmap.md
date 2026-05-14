# Video frame analysis — ML / GPU acceleration roadmap

> **Status:** Forward-looking research + staged plan. Documents what 2026
> open-source SOTA looks like for shot detection and video VLMs, maps it
> to this repo's selfhost economics, and proposes an opt-in adoption path.
> No code lives behind this yet — decision request at the bottom.

**Date:** 2026-05-14
**Builds on:** [`2026-05-12-video-frame-analysis-roadmap.md`](2026-05-12-video-frame-analysis-roadmap.md)
**Code under discussion:** [`ingest/src/pipeline/video_analysis.py`](../../ingest/src/pipeline/video_analysis.py)

---

## 1. Where we ended up after the May-13 work

Twelve commits on `claude/upgrade-scene-detection-ipcFv` upgraded the
scene-detection + extract path along an _orthogonal_ axis to the May-12
roadmap (which was about vision **grounding quality**; this batch was about
**signal quality + cost-of-extraction**). Quick recap of what now exists:

| # | Change | Effect |
|---|---|---|
| 1 | `AdaptiveDetector` as default | Robust to dissolves and pans |
| 2 | `min_scene_len` + `luma_only` knobs | Cheaper detection, no jitter cuts |
| 3 | Sample mid-scene, not at the cut | Avoids motion-blurred / fade frames |
| 4 | `thumbnail` filter over a small window | Picks the sharpest frame inside the seek window |
| 5 | Resize folded into ffmpeg call | One subprocess, not two |
| 7 | ThreadPoolExecutor over scenes | ~4× wall-clock for the extract stage |
| 11 | Half-sensitivity retry | Catches gentle dissolves before falling back to fixed-interval |
| 12 | `np.linspace` even-spread sampling | No more lost-tail bias on capped picks |
| 8 | `FFMPEG_HWACCEL` env (opt-in) | NVDEC / VAAPI / VideoToolbox decode |
| 9 | `ffmpeg_scdet` as 3rd algorithm | Native ffmpeg scene detector, several × faster than PySceneDetect |
| 10 | `silencedetect` cuts merged in | Audio-derived candidates for talky content |
| (6) | Single-pass batched ffmpeg call | **Skipped** — the math showed it loses to the parallel path on multi-core hosts |

**What's still on the table after this batch:**
- The vision call (Claude Sonnet 4.6) is now the longest pole in the
  wall-clock and the **only recurring per-video cost**.
- PySceneDetect, even at half sensitivity, still misses some slow
  dissolves and over-fires on flicker (low-FPS interview cameras).
- Everything is CPU-only by default. `FFMPEG_HWACCEL` can opt into
  hardware decode but nothing in the ML pathway uses the GPU.

This roadmap is about what we'd do _next_ if we wanted to push past
those limits.

---

## 2. The 2026 open-source landscape (verified May 2026)

### 2.1 Shot boundary detection

- **TransNetV2** (Souček & Lokoč, ACM Multimedia 2024) is the de-facto
  open baseline. CNN+LSTM over 100-frame windows. Available as
  `transnetv2-pytorch` on PyPI and as TF1/TF2 weights. ~50-100 fps on
  CPU, ~1000 fps on a single consumer GPU. Meaningfully better than
  PySceneDetect / ffmpeg `scdet` on _slow dissolves and fades_ — the
  exact cases where commit #11's half-sensitivity retry has to kick
  in. Source: https://github.com/soCzech/TransNetV2
- **AutoShot** (Zhu et al., CVPR 2023) beats TransNetV2 by ~4.2% F1 on
  the SHOT benchmark via NAS over 3D ConvNets and transformers.
  Newer, less battle-tested, no widely-used Python package yet.
  Source: https://arxiv.org/abs/2304.06116
- **2026 hybrid pattern in the literature:** PySceneDetect (or scdet)
  for fast initial cuts, TransNetV2 _only on dissolve-suspect regions_
  for fine-grained boundaries. We're already half-way there with the
  half-sensitivity retry — replacing the retry with a TransNetV2 pass
  is the natural extension.

### 2.2 Vision-language models (open-weights, self-hostable)

- **Qwen 3-VL** (Alibaba, 2025-2026) — current open-weights flagship.
  **Native video input up to 2-hour clips.** 256K-token context,
  expandable to 1M. The first open VLM that can plausibly replace the
  _entire_ scene-detect → extract → caption pipeline with one call.
  Sizes: 7B, 32B, 72B. The 7B fits a 24 GB consumer GPU (RTX 3090 /
  4090 / A10).
- **Pixtral 12B** (Mistral, Apache 2.0) — efficient image-VLM, no
  native video. Useful as a per-frame caption replacement; not for
  video-as-input.
- **Llama 3.2 Vision 11B / 90B** (Meta) — solid image-VLM with
  permissive license; competitive on captioning. The 90B needs serious
  GPU (≥80 GB), the 11B fits 24 GB.
- A 2026 video-embedding benchmark is explicit that **sampling 8
  frames + averaging SigLIP embeddings is weak for video** — for
  video-native tasks (retrieval, summarization), video-trained models
  (InternVideo2, Qwen 3-VL native-video) win. Implication: don't try
  to retrofit SigLIP-2 into a video summarizer; pick a video-native
  model instead.

### 2.3 NVIDIA video pipeline tooling

- **PyNvVideoCodec 2.0** ships a `ThreadedDecoder` whose explicit
  design goal is to "hide decoder latency behind inference" — exactly
  the I/O-bound pattern we have. Decode and ML inference run on the
  same GPU with no PCIe round-trip.
- **NVIDIA DALI** is the GPU-side preprocessing pipeline (decode →
  resize → augment → batch) used by most production CV training
  stacks. Pairs cleanly with PyNvVideoCodec.
- Both are open-source (Apache 2.0). They _replace_ the ffmpeg
  subprocess loop entirely with one in-process GPU pipeline.

---

## 3. ROI mapped to THIS repo

### 3.1 The economics

afffine-selfhost is a single-user (or small-team) selfhost. The
recurring per-capture spend today is approximately:

```
Whisper transcription (OpenAI):     ~$0.006 / minute audio
Sonnet 4.6 vision call:             ~$0.017 per video (4 keyframes,
                                                       ~3K in / 500 out tokens)
Sonnet 4.6 text render:             ~$0.05  per video
─────────────────────────────────────────────────────────
Per video, typical:                  ~$0.10 - $0.20
```

At common selfhost volumes (10-100 videos/day), that's **$30-$600/month**.

The breakeven for owning a dedicated cloud GPU box (RTX 4090 instance
on a marketplace ≈ $0.30-0.50/hr) is roughly **300 videos/day** if you
want to amortize the GPU. **Below that, the API is cheaper.**

The breakeven flips entirely if **the user already owns the GPU**
(gaming rig, prosumer workstation). Then self-hosting is pure win:
zero recurring cost, no rate limits, full privacy.

### 3.2 Latency vs cost vs effort

| Improvement | Latency saved | Recurring cost saved | Privacy gain | Effort | GPU required |
|---|---|---|---|---|---|
| TransNetV2 CPU as 4th algorithm | ~0 s (similar speed, better accuracy) | $0 | none | ~1 day | no |
| TransNetV2 GPU + SigLIP-2 keyframe quality scoring | 5-10 s/video | $0 | none | ~2 days | yes (8 GB+) |
| Self-host **Qwen 2.5-VL 7B** as VLM provider | 3-8 s/video | **all of it** | full | ~3-5 days | yes (24 GB) |
| Qwen 3-VL native-video; skip extract entirely | varies | all of it | full | ~1 week | yes (≥40 GB for 32B) |
| Full PyNvVideoCodec + DALI rewrite | several s | $0 | none | ≥2 weeks | yes |

### 3.3 What the user gets in exchange for the work

- **TransNetV2** — better cuts on dissolves (the case where current
  signal is weakest), at the cost of one extra optional dependency
  (`transnetv2-pytorch`, ~30 MB checkpoint). Same ~700 ms ingest
  budget on CPU; ~5× faster on GPU.
- **Self-hosted VLM** — kills the largest recurring API spend,
  removes the Anthropic data-flow concern, removes rate limits.
  Latency is comparable (vLLM with continuous batching matches API
  RTT for single-user). Adds operational burden: user runs vLLM
  separately, our repo just speaks the OpenAI-compatible HTTP schema.
- **Qwen 3-VL native-video** — collapses the pipeline. Instead of
  scene-detect → extract → upload → describe-each-frame, you just
  hand the model the mp4 and ask for "summary + N keyframes with
  timestamps". Architecturally simpler; algorithmically equivalent
  or better. The downside is wall-clock: a single 7B-parameter
  forward pass over a 30-min video is 20-60s on a 4090.
- **PyNvVideoCodec / DALI** — would only matter at scale. Below
  ~100 videos/day the engineering cost dwarfs the latency savings.

---

## 4. Proposed staged plan

Each stage is independently shippable and reversible. Earlier stages
do not block later ones.

### Stage A — TransNetV2 as a fifth `SCENEDETECT_ALGORITHM` value (~1 day, no GPU)

**Why first.** Smallest blast radius. Same code shape as the existing
`adaptive` / `content` / `ffmpeg_scdet` switch we already added in #9.
Eliminates the hacky half-sensitivity retry (#11) for the cases where
the original detector misses dissolves. Works on whatever hardware the
operator already has — uses GPU if `torch.cuda.is_available()`,
gracefully falls back to CPU.

**Scope.**
1. New optional dep: `transnetv2-pytorch` (kept out of the base image;
   loaded behind a try/except like PySceneDetect already is).
2. `_detect_scene_timestamps_transnetv2(video_path)` returns scene
   midpoints, plugged into the existing dispatcher in
   `_detect_and_extract_frames`.
3. New env: `SCENEDETECT_TRANSNET_THRESHOLD=0.5` (model probability
   threshold for a cut).
4. Auto-device selection: `torch.cuda.is_available()` → `cuda`, else
   `cpu`. No new config knob.
5. Tests: regex/parse tests with mocked model output.
6. Doc update: README mentions the option + its dep.

**Out of scope.** No PySceneDetect removal. No half-sensitivity-retry
removal (different algorithm, leave the existing one alone). No
hybrid PySceneDetect+TransNetV2 path — keep it simple, one algo at a
time, operator picks.

**Decision deferred to implementation.** Whether to vendor the
checkpoint in the Docker image (~30 MB, predictable cold-start) vs
download on first use (smaller image, first run is slow). Lean toward
vendor — selfhost users hate first-run surprises.

### Stage B — Pluggable self-hosted VLM provider (~3-5 days)

**Goal.** Operators with a GPU swap Anthropic's vision API for a
locally-served Qwen / Pixtral / Llama Vision model without changing
the rest of the pipeline.

**Mechanism.**
1. New config: `VISION_PROVIDER=claude` (default) | `local_openai`.
2. `local_openai` reads `VISION_BASE_URL`, `VISION_MODEL`,
   `VISION_API_KEY` (optional bearer for vLLM auth).
3. Refactor `_vision_call()` in `video_analysis.py`: extract a
   provider-agnostic message-shape building step, then dispatch to
   either the existing Anthropic SDK call or a generic
   OpenAI-compatible HTTP POST.
4. The OpenAI-shape adapter handles the image_url base64 encoding
   that vLLM / Ollama / lmdeploy all accept.
5. **No model code in this repo.** vLLM runs as a separate service
   (operator's responsibility); the repo gains an HTTP client and a
   config switch, nothing else.
6. README gains a "Self-hosting the vision model" section with the
   exact `vllm serve Qwen/Qwen2.5-VL-7B-Instruct` command.

**Out of scope.** Streaming responses (we don't stream today;
synchronous response is fine). Tool use against the local model
(closed-API specific). Prompt-caching equivalence — local models
don't have it; document the gap.

**Tests.** Mock `httpx.AsyncClient.post`; verify request shape and
response parsing. No real model needed in CI.

### Stage C — Qwen 3-VL native-video collapse (~1 week, ≥40 GB GPU)

**Goal.** Skip scene-detect + extract entirely. Hand the mp4 to a
video-native VLM, get back `{summary, keyframes: [{t, caption}, ...]}`
in one call.

**Why this is a bigger lift.**
1. The orchestrator currently routes captures through
   `cobalt_ext.extract()` which depends on the extracted-frames-as-blobs
   contract. Collapsing the pipeline changes that data shape.
2. Keyframe blobs still need to exist for AFFiNE to render `kf:N`
   refs. So we'd need the model to _identify_ keyframe timestamps,
   then we still ffmpeg-extract those specific timestamps and upload.
   This is actually fine — moves the "which timestamp is interesting"
   decision into the VLM, but keeps the upload path.
3. The vision call's response schema would change. Snapshot
   compatibility (`extracted.extra["keyframes"]`) needs care.

**Defer until Stages A and B are stable.** This is the architecturally
exciting one but also the highest-risk. Not worth doing without a
real GPU on the bench to measure.

### Stage D — Full GPU pipeline (PyNvVideoCodec + DALI) — _not recommending_

Only worth pursuing if the repo grows into multi-tenant /
serve-many-users-from-one-box territory. Below ~100 videos/day the
engineering cost dwarfs the latency savings. Document as known
option for future scale-up; do not ship without a clear scale need.

---

## 5. Recommendation

If we ship anything, ship Stage A. It's the lowest-risk, lowest-cost
win, validates the "configurable detection backend" abstraction we
already built, and benefits every operator regardless of hardware.

If the user is willing to run vLLM separately, Stage B is the highest
ROI in $$ saved per hour of engineering — for any operator with a
24 GB GPU it pays for itself in days at typical selfhost volumes.

Stage C waits until A and B settle. Stage D waits until scale demands
it (which it currently doesn't).

---

## 6. Open questions

- **Dependency tolerance.** Are we willing to add `torch` (~1 GB
  Docker layer) to the base image to enable Stage A out of the box,
  or stay with the optional-import pattern (smaller image, opt-in
  install)? Recommendation: optional-import. Selfhost users with
  existing torch installs already win; users without one don't pay
  the size cost.
- **Checkpoint distribution.** Vendor TransNetV2 weights in the
  image (~30 MB) vs download on first use? Recommendation: vendor.
- **Prompt-caching gap.** Anthropic's prompt cache is the reason the
  per-render Sonnet text call is cheap; vLLM doesn't have an
  equivalent. If we move text rendering to a local VLM later (NOT in
  this roadmap), per-token cost goes up, not down. Document the
  trade-off when Stage B ships so operators don't migrate the text
  path naively.
- **GPU-aware Docker compose profile.** Should we ship a
  `docker-compose.gpu.yml` overlay that runs vLLM alongside the
  ingest service? Probably yes when Stage B lands — without it, the
  "easy self-host" promise breaks.
- **Privacy framing in README.** Current README understates that
  Whisper + Anthropic both see user-captured content. Stage B
  materially changes this story for operators who adopt it; the
  README should explain the trade and how to verify the local VLM
  isn't phoning home.

---

## 7. What changed since the May-12 roadmap

The May-12 roadmap (Tiers 1-7) is about **content / quality**
improvements: better keyframe placement, OCR, template-aware
re-ranking, diarization. Those tiers are still the right things to
ship for product quality.

This roadmap is about a **different axis**: detection accuracy and
infra cost. The two roadmaps are independent — Stage A here doesn't
collide with Tier 5 there (frame-quality pre-filter); they actually
compose well (better cuts → fewer junk frames to filter).

| May-12 roadmap | This roadmap |
|---|---|
| What does the LLM see? | What hardware decodes / detects / captions? |
| Cost lever: prompt design, OCR, re-ranking | Cost lever: kill the API call entirely |
| Universal — works on any host | Stage B+ requires user-supplied GPU |

Pick from each independently.

---

## 8. Decision request

Tell me which stage(s) to ship next and I'll write the focused
implementation plan. Most likely order based on cost-vs-impact:

**Stage A → Stage B → (Stage C if you have the GPU and the appetite).**

Sources used to verify the 2026 landscape (May 2026):
- TransNetV2 / AutoShot benchmarks: https://github.com/soCzech/TransNetV2 ;
  https://arxiv.org/abs/2304.06116
- 2026 open-source VLM landscape: https://www.bentoml.com/blog/multimodal-ai-a-guide-to-open-source-vision-language-models ;
  https://localaimaster.com/blog/qwen-3-vl-local-setup
- PyNvVideoCodec 2.0 ThreadedDecoder: https://developer.nvidia.com/blog/whats-new-in-pynvvideocodec-2-0-for-python-gpu-accelerated-video-processing/
- 2026 video embedding benchmark: https://mixpeek.com/blog/video-embedding-benchmark-2026
