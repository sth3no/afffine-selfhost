# GPU offload, OCR & power-managed home node — macro plan (Phases 19-21)

> **Status:** Forward-looking macro plan. Phase 19 (PaddleOCR) is
> self-contained and shippable on the VPS alone. Phase 20 (remote GPU
> worker) is the architectural piece — it lets a GPU box you already
> own (a gaming PC) do the heavy ML while the VPS stays a cheap
> always-on orchestrator. Phase 21 (power-managed home node) makes that
> gaming PC sleep when idle and wake on demand — for both the GPU
> compute AND a residential proxy running on the same box. Decision
> request at the bottom.

**Date:** 2026-05-16
**Builds on:**
- [`2026-05-14-phase-17-transnetv2-detector.md`](2026-05-14-phase-17-transnetv2-detector.md) — scene detection.
- [`2026-05-14-phase-18-transcript-guided-keyframes.md`](2026-05-14-phase-18-transcript-guided-keyframes.md) — transcript → ranking → frame selection.
- [`2026-05-14-video-analysis-ml-acceleration-roadmap.md`](2026-05-14-video-analysis-ml-acceleration-roadmap.md) — Stage B/D (self-hosted GPU compute).
- [`2026-05-12-video-frame-analysis-roadmap.md`](2026-05-12-video-frame-analysis-roadmap.md) — Tier 3 (OCR) is the direct predecessor of Phase 19.

---

## 1. The target pipeline

The full per-video flow, with what's already shipped marked `[DONE]`:

```
URL
 │
 ├─ Phase 1  Whisper / YT-captions transcription            [DONE: phase 13/18]
 │             → transcript + per-segment timestamps
 │
 ├─ Phase 2  text-only LLM ranks speech windows             [DONE: phase 18]
 │             → (importance, visual_anchor) per ~45s window
 │
 ├─ Phase 3  scene detect (adaptive / scdet / transnetv2)   [DONE: phase 13/17]
 │             → candidate cut timestamps
 │
 ├─ Phase 3b transcript-guided candidate filter             [DONE: phase 18]
 │             → keep frames in high-anchor windows
 │
 ├─ Phase 3c frame-quality pre-filter                       [DONE: phase 16]
 │             → drop black / uniform / duplicate frames
 │
 ├─ Phase 4  Claude vision call                             [DONE: phase 13]
 │             → grounded summary + per-frame caption + importance
 │
 ├─ Phase 5  ON-SCREEN OCR  (PaddleOCR)                     [NEW: phase 19]
 │             → exact text of slides / code / charts per keyframe
 │
 └─ Phase 6  blob upload + AFFiNE render                    [DONE: phase 13/14]
               → keyframes + ocr_text woven into the doc body

Cross-cutting: Phase 5 (and optionally Phase 3/4) can run on a
remote GPU box instead of the VPS                            [NEW: phase 20]

Cross-cutting: that GPU box (a gaming PC) sleeps when idle and
is woken on demand — for GPU jobs AND a residential proxy        [NEW: phase 21]
running on the same machine
```

Phases 19 and 20 are independent: Phase 19 ships PaddleOCR running on
the VPS (CPU). Phase 20 then lets that OCR — and later other ML stages
— offload to a GPU machine. You can ship 19 alone and add 20 later.

---

## 2. Phase 19 — PaddleOCR on-screen text extraction

### 2.1 Goal

The vision call (Phase 4) describes a frame in prose ("an IDE showing
React code", "a slide titled 'Why AI struggles with Swift'") but does
NOT transcribe the actual text. For technical content — tutorials with
code, talks with data slides, documentaries with diagram callouts —
the *substance* on screen is the most valuable signal and right now
it's invisible: the user sees the image but can't search, copy, or
quote it.

Phase 19 runs OCR on text-rich keyframes and stores the transcribed
text on the keyframe so it can be (a) searched, (b) quoted in `body_md`
even when the image itself isn't embedded, (c) fed back to the
template render.

### 2.2 Where OCR sits

OCR runs AFTER the vision call (Phase 4), on the *kept* keyframes only
— typically 2-6 frames, not all 12 candidates. This is deliberate: OCR
is not free, and the vision call has already told us which frames are
worth keeping. No point OCR-ing a frame that got dropped.

### 2.3 Trigger logic

Don't OCR every kept frame — a frame that's just a person talking has
no text worth extracting. Two options:

- **(a) Caption keyword match** — scan the Phase 4 caption for `slide`,
  `code`, `chart`, `diagram`, `text`, `title`, `screen`, `UI`. Cheap,
  zero schema change, but brittle (depends on the caption's wording).
- **(b) Vision model flags it directly (recommended).** Extend the
  `_FrameCaption` schema with one field:
  ```python
  class _FrameCaption(BaseModel):
      frame_index: int
      caption: str
      importance: int
      text_density: int = Field(ge=0, le=10, description=
          "0 = no readable text on screen. 10 = frame is dominated by "
          "text/code/slide content worth transcribing verbatim.")
  ```
  The vision model is already looking at the frame — asking "how much
  readable text is here" is one extra integer and far more reliable
  than keyword-matching its prose. OCR fires when `text_density >=
  settings.ocr_text_density_threshold` (default 4).

Recommendation: **(b)**. Additive schema change, backwards compatible
(old snapshots have no `text_density` → treated as 0 → no OCR, which is
the safe default).

### 2.4 The OCR provider abstraction

`OCR_PROVIDER` config, three values:

| Value | Behavior | When |
|---|---|---|
| `none` | OCR disabled entirely (default until Phase 19 is proven) | Operators who don't want it |
| `local_paddle` | PaddleOCR runs in-process on whatever host the ingest service runs (VPS, CPU). | Phase 19 baseline — works everywhere, no GPU box needed |
| `gpu_worker` | OCR job dispatched to the remote GPU worker (Phase 20). Falls back to `cloud_claude` then `none` if the worker is unreachable. | Phase 20 — needs the gaming-PC node |
| `cloud_claude` | A second Claude vision call with a "transcribe all text in this image verbatim" prompt. | Fallback / operators with no GPU and no appetite for the PaddleOCR dependency |

Phase 19 ships `none`, `local_paddle`, and `cloud_claude`. Phase 20
adds `gpu_worker`.

PaddleOCR notes:
- Apache 2.0, Baidu. PP-OCRv5 models. 80+ languages incl. Czech,
  English; handles rotated text and dense layouts well.
- Optional dependency — `paddleocr` + `paddlepaddle` (CPU) kept out of
  the base image, loaded behind a try/except like `scenedetect` and
  `transnetv2-pytorch` already are.
- CPU inference on the VPS: ~0.3-1.5s per 1024px frame. For 2-6
  keyframes that's a few seconds — acceptable given the user's stated
  "images are the last thing rendered, latency doesn't matter".
- The GPU path (Phase 20) is ~10-30x faster but Phase 19 doesn't need it.

### 2.5 Schema + render integration

- `KeyframeRef.ocr_text: str | None` — **already exists** in
  `video_analysis.py` (reserved during the Phase 16 work). Phase 19
  just starts populating it.
- `extracted_snapshot.keyframes` JSONB already round-trips arbitrary
  keyframe fields — `ocr_text` rides along, backwards compatible (older
  snapshots have `null`).
- Render: the template-render user message gains the OCR text under
  each keyframe, exactly as the May-12 roadmap Tier 3 sketched:
  ```
  Available keyframes:
    [0] t=42.3s — IDE with React code
        OCR: "function useEffect(callback, deps) { ... }"
    [1] t=154.0s — Slide "Why AI struggles with Swift"
        OCR: "1. Data Gap   2. API Drift   3. Benchmarking Bias"
  ```
  The template LLM can then quote the slide/code verbatim in `body_md`
  even when it chooses not to embed the image.

### 2.6 Scope

| File | Change |
|---|---|
| `ingest/src/config.py` | `ocr_provider`, `ocr_text_density_threshold`, `ocr_max_frames`, `ocr_languages`. |
| `ingest/src/pipeline/video_analysis.py` | Extend `_FrameCaption` with `text_density`. New `_run_ocr(kept_frames)` step after the vision call; populates `KeyframeRef.ocr_text`. |
| `ingest/src/pipeline/video_ocr.py` (new) | The provider abstraction: `local_paddle` (PaddleOCR), `cloud_claude` (Claude vision). `gpu_worker` is a stub raising "not available until Phase 20". |
| render layer | Surface `ocr_text` in the template user message. |
| tests | OCR trigger logic, provider dispatch, mocked PaddleOCR, graceful skip when dep missing. |

No migration (JSONB is additive). No new base-image dependency
(optional import).

### 2.7 Tests

All mock-driven — no real PaddleOCR / GPU / Claude in CI. Cover:
trigger threshold, `local_paddle` dispatch with mocked engine,
`cloud_claude` dispatch, missing-dependency graceful skip, `ocr_text`
reaching the snapshot + render message.

---

## 3. Phase 20 — Remote GPU worker (the gaming PC)

### 3.1 The problem

The VPS is cheap, always-on, but has **no GPU**. A gaming PC has a
strong GPU but **isn't a server**: it's behind home NAT (can't accept
inbound connections), it isn't always on, and it's sometimes busy
gaming. We want to use its GPU for the heavy ML (OCR now; TransNetV2 /
a local VLM later) WITHOUT pretending it's a reliable server.

### 3.2 Architecture — pull-model job queue

The gaming PC must **reach out** to the VPS — never the other way
round — because of home NAT. The cleanest fit, and consistent with the
existing `captures` queue (`db.py:claim_next_queued` already uses
`FOR UPDATE SKIP LOCKED`), is a **pull-model job queue**:

```
        VPS (always on)                      Gaming PC (sometimes on)
   ┌──────────────────────┐              ┌───────────────────────────┐
   │ capture worker       │              │ gpu-worker agent          │
   │  ├ enqueue gpu_job ───┼──> gpu_jobs  │   loop:                   │
   │  │   (kind=ocr,       │    table     │     POST /gpu/jobs/claim ─┼─┐
   │  │    payload=frames) │      ▲       │     run PaddleOCR on GPU  │ │
   │  └ poll for result <──┼──────┘       │     POST /gpu/jobs/result─┼─┘
   │       (deadline)      │              │   sleep(poll_interval)    │
   └──────────────────────┘              └───────────────────────────┘
        no inbound needed                  only outbound HTTPS — works
                                           behind any home NAT
```

- New `gpu_jobs` table: `id`, `kind` (`'ocr'`), `status`
  (`pending`/`claimed`/`done`/`failed`), `payload` jsonb, `result`
  jsonb, `created_at`, `claimed_at`, `claimed_by`, `finished_at`,
  `attempts`.
- Worker claim endpoint runs the same `FOR UPDATE SKIP LOCKED` claim
  the capture queue already uses — proven pattern, no new concurrency
  surface.
- Stale-claim recovery: a job `claimed` longer than
  `gpu_job_claim_timeout` with no result → reset to `pending` (or
  `failed` after `gpu_job_max_attempts`). Covers "user launched a game,
  agent got killed mid-job".

### 3.3 The gpu-worker agent

A new top-level `gpu-worker/` directory — a small standalone Python
program, NOT part of the ingest service:

- Config: `INGEST_URL`, `GPU_WORKER_TOKEN`, `POLL_INTERVAL_SECONDS`,
  device selection.
- Loop: claim → dispatch by `kind` → post result. One handler per
  job kind; `ocr` is the only handler in Phase 20, but the dispatch
  is a dict so TransNetV2 / VLM handlers slot in later with no
  rearchitecting.
- Packaging: a Python venv + `pip install paddlepaddle-gpu paddleocr`
  is the path of least resistance on a Windows gaming PC (Docker +
  GPU passthrough on Windows is finicky). A CUDA Docker image is
  offered for Linux GPU boxes.
- Runs as a background service / tray app / scheduled task. Optional
  niceness: pause polling while a fullscreen game is detected.

### 3.4 Graceful degradation — non-negotiable

A gaming PC is **not a reliable server**. Every GPU job MUST have a
fallback chain. For OCR:

```
gpu_worker  ──(worker offline / job times out)──>  cloud_claude
            ──(no Claude budget / disabled)─────>  local_paddle (VPS CPU)
            ──(dependency missing)──────────────>  none (skip; ocr_text stays null)
```

The capture flow enqueues the OCR job, waits up to
`gpu_job_wait_deadline` seconds (default 120 — fine given OCR is the
last thing rendered), and on timeout walks down the fallback chain.
The doc still renders; it just renders without `ocr_text` in the worst
case. **No capture ever blocks forever on an offline gaming PC.**

### 3.5 Transport — job queue vs Tailscale

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Pull job queue** (above) | Zero network config; works behind any NAT; consistent with existing `captures` queue; survives the gaming PC being off | Poll-interval latency (~2-5s) | **Recommended primary** |
| **Tailscale / WireGuard mesh** | VPS calls the gaming PC directly; low latency; encrypted | User must install Tailscale on both boxes (easy in 2026, but a step) | Optional low-latency upgrade — the agent can register its tailnet IP and the VPS calls it directly, skipping the poll |
| Reverse tunnel (cloudflared/ngrok) | Works | Third-party dependency in the hot path | Not recommended |

Phase 20 ships the **pull job queue**. Tailscale is documented as an
optional upgrade — the job queue stays as the fallback even when
Tailscale is up (gaming PC off → jobs queue → cloud fallback).

### 3.6 Security

- The gaming PC authenticates to the VPS with a dedicated
  `GPU_WORKER_TOKEN` — separate from the capture API token, separate
  from the browser-extension token. Revocable independently.
- **The GPU worker never receives user cookies.** OCR needs only the
  frame image. This deliberately keeps the gaming PC out of the
  sensitive-credential blast radius discussed in the productization
  conversation — a breached gaming PC leaks frames, not Google
  sessions.
- Job payloads (frame JPEGs) travel VPS→worker as base64 in the job
  `payload` jsonb. Frames are ~500 KB; 2-6 per video is a small,
  short-lived payload. No separate blob-serving endpoint needed.
- All transport over HTTPS. The VPS validates the worker token on
  every claim / result call.

### 3.7 Scope

| Component | Change |
|---|---|
| `ingest` migration | New `gpu_jobs` table. |
| `ingest/src/db.py` | `enqueue_gpu_job`, `claim_next_gpu_job` (SKIP LOCKED), `complete_gpu_job`, `recover_stale_gpu_jobs`. |
| `ingest/src/api.py` | `POST /gpu/jobs/claim`, `POST /gpu/jobs/{id}/result`, authed by `GPU_WORKER_TOKEN`. |
| `ingest/src/pipeline/video_ocr.py` | Implement the `gpu_worker` provider: enqueue + wait + fallback chain. |
| `gpu-worker/` (new top-level) | The standalone agent: config, claim loop, OCR handler, packaging (venv + Docker). |
| `ingest/src/config.py` | `gpu_worker_token`, `gpu_job_wait_deadline`, `gpu_job_claim_timeout`, `gpu_job_max_attempts`. |
| tests | Queue claim/complete/recover; API auth; provider fallback chain; agent claim-loop with mocked PaddleOCR. |

### 3.8 Tests

VPS side: `gpu_jobs` enqueue/claim/complete/stale-recovery; endpoint
auth (wrong token rejected); the `gpu_worker` provider's fallback chain
(worker timeout → cloud → local → none). Agent side: claim-loop with a
mocked PaddleOCR engine, result POST shape. All mock-driven — CI needs
no GPU.

---

## 4. Phase 21 — Power-managed home node

### 4.1 The problem

The home node (gaming PC) now has two jobs — PaddleOCR GPU compute
(Phase 20) and a **residential proxy** for YouTube downloads (already
set up by the operator on the same box). It should be **OFF when idle**
— it's a gaming PC, not a server: power, fan noise, hardware wear — and
**wake on demand** for either consumer. Two hard constraints make this
non-trivial:

- **WoL can't cross the internet.** A Wake-on-LAN magic packet is a LAN
  broadcast; routers don't forward it. The VPS therefore cannot wake
  the gaming PC directly — an always-on device on the home LAN must
  relay the packet.
- **The proxy lives on the gaming PC.** It can't wake itself. The relay
  must be a separate always-on box.

Resolved hardware (from the design discussion): a **Raspberry Pi** is
the wake relay; the gaming PC runs **Windows**.

### 4.2 Component A — wake relay (Raspberry Pi)

A tiny always-on service on the Pi (`wake-relay/`, new top-level dir):

- `POST /wake` → sends the WoL magic packet to the gaming PC's MAC on
  the local LAN.
- `GET /status` → pings the gaming PC, reports reachable / not.
- Joined to the **Tailscale** tailnet so the VPS calls it directly by
  its tailnet IP — no port-forwarding, no dynamic DNS, encrypted.
- Authenticated with a dedicated `WAKE_RELAY_TOKEN`.
- The Pi draws ~2-3 W always-on — negligible. systemd restart-on-crash.
- *(An ESP32 variant can't run Tailscale — it would instead poll the
  VPS for pending wake requests. Documented as the lighter alternative;
  the Pi path is the recommended one.)*

### 4.3 Component B — Windows agent power management

The `gpu-worker` agent (Phase 20) gains power-management duties:

- **Heartbeat** — while awake, POSTs a heartbeat to the VPS every ~30 s
  so the VPS knows the node is READY / BUSY and when it was last seen.
- **Idle-detection loop** — every 30 s, the agent suspends the PC only
  when ALL of these hold:
  - no OCR job claimed / running, AND
  - the proxy has **zero active connections**, AND
  - no user input for `idle_user_minutes` (Win32 `GetLastInputInfo` —
    never sleep a PC someone is actively using), AND
  - it's been at least `wake_grace_minutes` since the last wake (so a
    fresh wake can't immediately re-sleep before the job lands).
- **Suspend** — S3 (suspend-to-RAM) via
  `rundll32.exe powrprof.dll,SetSuspendState 0,1,0`.
- **Proxy-activity signal** — the agent counts ESTABLISHED sockets on
  the proxy's listen port (or reads the proxy's stats endpoint if it
  exposes one), so a download in flight pins the PC awake.

Windows setup the README must spell out:
- Disable **Fast Startup** — it turns shutdown into a hybrid state that
  breaks WoL. (We use S3 sleep anyway, which sidesteps this, but the
  setting still interferes and should be off.)
- NIC properties: enable "Allow this device to wake the computer" +
  "Only allow a magic packet to wake the computer".
- BIOS: enable Wake-on-LAN / Power-On by PCI-E.

### 4.4 Component C — VPS `ensure_home_node_awake()`

One primitive that both the OCR dispatch (Phase 20) and the
proxy-routed download path call before they need the node:

```
ensure_home_node_awake(timeout = home_node_wake_timeout):
    status ← derived from the last agent heartbeat
    if READY or BUSY:        return OK
    if WAKING:               wait for READY (timeout)
    else (SLEEP / stale):
        POST <wake-relay>/wake
        poll heartbeat until READY (timeout)
        if not READY in time: return FAILED   → caller does its fallback
```

Node status is derived from the heartbeat freshness: a recent
heartbeat → READY/BUSY; a stale one → assumed asleep.

### 4.5 The two consumers

- **OCR (Phase 20).** Before enqueueing an OCR `gpu_job`, the capture
  flow calls `ensure_home_node_awake()`. Wake fails → the existing
  Phase 20 fallback chain (`cloud_claude` → `local_paddle` → `none`).
- **Proxy-routed download.** Before routing a YouTube download through
  the home proxy, the download path calls `ensure_home_node_awake()`.
  Wake fails → fall back to the current VPS-direct cobalt+cookies path.
  **Note:** the actual wiring that makes cobalt / yt-dlp *use* the home
  proxy is a separate concern (call it Phase 22). Phase 21 delivers
  only the wake/sleep orchestration and the `ensure_home_node_awake()`
  primitive that Phase 22 would consume.

### 4.6 State machine

```
 SLEEP(S3) ──wake packet──▶ WAKING ──agent heartbeat──▶ READY
    ▲                                                     │
    │                                          OCR job / proxy conn
    │                                                     ▼
    └─── idle N min: no job, no proxy, no user ◀── IDLE ◀── BUSY
                                               (countdown)
```

### 4.7 Scope

| Component | Change |
|---|---|
| `wake-relay/` (new top-level) | Tiny Pi service: `/wake`, `/status`, Tailscale join, token auth, systemd unit. |
| `gpu-worker/` (Phase 20 dir) | Add heartbeat, idle-detection loop, Windows S3 suspend, proxy-connection monitor, `GetLastInputInfo` user-activity check. |
| `ingest/src/api.py` | `POST /home-node/heartbeat` (agent → VPS). |
| `ingest/src/...` | `ensure_home_node_awake()` primitive + node-status tracking from heartbeats; called by the OCR dispatch. |
| `ingest/src/config.py` | `wake_relay_url`, `wake_relay_token`, `home_node_wake_timeout`, `idle_suspend_minutes`, `idle_user_minutes`, `wake_grace_minutes`. |
| tests | Wake primitive (already-awake / wake-success / wake-timeout→fallback); idle-detection (jobs / proxy / user each pin awake); heartbeat staleness → assumed-asleep. |

### 4.8 Risks

| Risk | Mitigation |
|---|---|
| WoL flaky (NIC firmware, Windows config) | `ensure_home_node_awake()` has a timeout → caller falls back. Wake is never on a no-fallback critical path. |
| Agent sleeps the PC mid-download | Idle detection counts proxy connections; a download in flight pins the PC awake. |
| User is gaming, PC sleeps under them | `GetLastInputInfo` user-activity check — never sleep an in-use PC. |
| Pi relay itself down | VPS treats wake as failed → fallback. Pi is simple + low-power; systemd restarts it on crash. |
| Wake → immediate re-sleep before the job arrives | `wake_grace_minutes` hold-off after every wake. |
| Long wake latency (S3 ~5 s, cold boot ~30-60 s) on the first request after idle | Captures are async 30-60 s+ jobs already; front-loaded wake latency is acceptable. S3 sleep keeps it to ~5 s. |

---

## 5. Sequencing

1. **Phase 19 first, in full.** Self-contained, ships real value
   (searchable on-screen text) on the VPS alone, proves the OCR
   provider abstraction. ~2-3 days.
2. **Phase 20 second.** The distributed-compute layer on top of a
   working OCR step. ~4-6 days. Splittable: 20a = `gpu_jobs` queue +
   API + provider; 20b = the `gpu-worker` agent + packaging.
3. **Phase 21 third.** Power management on top of a working GPU
   worker. ~3-5 days. Splittable: 21a = `wake-relay/` Pi service +
   `ensure_home_node_awake()` + heartbeat (independent, can start in
   parallel with 20b); 21b = the Windows agent's idle-detection +
   suspend logic (extends the Phase 20 agent, so needs 20b done).
4. Don't start Phase 20 until Phase 19's `local_paddle` path is proven
   on real captures — the GPU worker is just a faster executor of the
   same OCR work, so the work itself must be correct first. Don't start
   Phase 21b until the Phase 20 agent exists to extend.

---

## 6. Open questions

- **Which keyframes to OCR.** Recommendation §2.3(b) — vision model
  emits `text_density`. Confirm before implementing the schema change.
- **OCR-blocks-capture vs async re-render.** Phase 20 blocks the
  capture worker up to `gpu_job_wait_deadline`. Acceptable per your
  "images render last, latency doesn't matter". The cleaner-but-bigger
  alternative — render the doc first, patch in `ocr_text` via a later
  block-update — needs the rerender path and is deferred. OK to start
  with blocking?
- **Multiple GPU workers.** The queue supports N workers out of the
  box (SKIP LOCKED). Not needed now, but means a second box (or a
  cloud GPU spot instance during a backlog) can join with zero code
  change.
- **Proxy stats interface (Phase 21).** The idle-detector needs to
  know "is the proxy in use". Cleanest if the proxy exposes a stats
  endpoint; otherwise the agent counts ESTABLISHED sockets on the
  proxy port. Which proxy software is it — does it have a stats API?
- **Wake-relay reachability (Phase 21).** Plan assumes the Pi joins
  Tailscale so the VPS can call it. Confirm Tailscale is acceptable on
  the home network (it's the cleanest; the alternative is port-forward
  + dynamic DNS).

**Resolved in the design discussion:** wake relay = Raspberry Pi;
gaming PC OS = Windows (S3 suspend via `SetSuspendState`, WoL via a
magic packet from the Pi).

## 7. Future extensions (explicitly NOT in 19-21)

Once the GPU worker exists, it's a general GPU job runner. Natural
follow-ups, each just a new job `kind` + handler:

- **TransNetV2 on the GPU worker.** Phase 17's detector is ~10-20x
  faster on GPU. Complication: the detector needs the *whole video*,
  not a few frames — shipping a multi-hundred-MB file to the gaming PC
  over home upload bandwidth is the open problem. Possible answer:
  let the gaming PC download the video itself (see next point).
- **Local VLM on the GPU worker.** May-14 Stage B/C — run Qwen 3-VL on
  the gaming PC and kill the recurring Claude vision spend entirely.
  The `gpu_worker` provider pattern from Phase 20 generalizes straight
  to a `vision` job kind.
- **Phase 22 — residential-IP download through the home proxy.** The
  gaming PC runs a proxy on a *residential IP* — the exact thing
  YouTube's bot detection treats leniently (per the cobalt / Raycast
  discussion). Phase 22 wires cobalt / yt-dlp to route the download
  through that proxy, gated by `ensure_home_node_awake()` from
  Phase 21. Sidesteps the VPS's datacenter-IP problem entirely.
  **Optional — the current VPS-direct cobalt+cookies path stays the
  default and the fallback.** Phase 21 already builds the wake
  primitive Phase 22 needs, so Phase 22 becomes a small wiring change.
- **TransNetV2 on the GPU worker.** Phase 17's detector is ~10-20x
  faster on GPU — a new `transnetv2` job `kind`. Complication: it
  needs the *whole video*. Once Phase 22 exists (the gaming PC
  downloads the video itself, on its residential IP), the video is
  already local to the GPU box and this complication disappears.
- **Local VLM on the GPU worker.** May-14 Stage B/C — run Qwen 3-VL on
  the gaming PC and kill the recurring Claude vision spend entirely.
  The `gpu_worker` provider pattern generalizes straight to a
  `vision` job `kind`.

## 8. Decision request

Confirm and I'll write the focused Phase 19 implementation plan
(the single-phase, file-by-file doc), then implement it. Recommended
order: **Phase 19 (PaddleOCR, VPS-local) → Phase 20a (GPU job queue)
→ Phase 20b (gpu-worker agent) → Phase 21a (wake relay +
`ensure_home_node_awake`) → Phase 21b (Windows agent idle/suspend).**

Open questions in §6 worth answering before code starts — especially
the `text_density` schema decision (Phase 19) and which proxy software
runs on the gaming PC (Phase 21 idle-detection).
