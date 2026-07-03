# Ingest Service — Review & Next Steps

**Status:** Proposal · **Date:** 2026-07-03 · **Scope:** `ingest/` (+ CI/deploy posture)

A follow-up review of the ingest service, one month after the 2026-06-10
improvement batch (PR #72). Everything below was verified by reading the
current code on `main` and running the test suite (433 passed, 9 skipped,
<5 s — after clearing proxy env vars, see finding N3).

---

## Where the system stands

The June batch landed cleanly. Verified in code:

| June item | Status | Where |
|---|---|---|
| #1 worker survives DB hiccups, honest /health | ✅ | worker.py:130-139, api.py:237-259 |
| #2 snapshot-first extraction, reuse on retry | ✅ | orchestrator.py:77-118 |
| #3 concurrent-duplicate /capture race + orphan cleanup | ✅ | api.py:438-449 |
| #4 per-capture timeout | ✅ | worker.py:155-171 |
| #5 error/completed_at surfaced in API | ✅ | db.py:44-50, api.py:993-1007 |
| #6 queue_depth excludes permanent failures | ✅ | db.py:249-260 |
| #7 /capture survives AFFiNE downtime, deferred stub | ✅ | api.py:397-418, orchestrator.py:62-75 |
| #8 shared `build_doc_blocks` layout | ✅ half | orchestrator.py:240 — replace semantics still open |
| #9 worker concurrency (2 loops) + folder lock | ✅ half | api.py:161-164, filer.py:66 — per-query pool acquisition still open |
| #10 wake event (no 2 s pickup latency) | ✅ | worker.py:100-103, api.py:452 |
| #11 shared LLM SDK clients | ✅ half | llm_clients.py — no explicit timeout/max_retries yet |
| #12 classifier structured outputs | ✅ | classifier.py:120-132 |
| #13 cost accounting | ❌ open | nothing reads `response.usage` anywhere |

The pipeline is in good shape: state machine is crash-safe, retries are
cheap (snapshot + classifier reuse), the YouTube extraction path has
layered fallbacks (captions → cobalt+Whisper → oEmbed-only), and the
test suite is broad and fast.

The rest of this doc is (a) new findings from this review, ordered by
priority, and (b) a recommended execution order.

---

## New findings

### N1 — Whisper cost guard is not enforced on the cobalt path (P0) — ✅ SHIPPED 2026-07-03

> Implemented on this branch: duration gate from yt-dlp metadata before
> the audio download, mid-stream abort at the 25 MB Whisper upload cap
> (duration-unknown case), pre-upload size backstop in
> `_whisper_transcribe`, `transcript_source: "skipped_too_long"` +
> `transcript_unavailable: true` on skipped captures.

`MAX_TRANSCRIPT_MIN` (default 30) is enforced only in the legacy `ytdlp`
extractor (ytdlp_ext.py:79-81) — **which no platform in topics.yaml uses
anymore**. Every video platform (youtube, instagram, tiktok, x, vimeo)
routes through `cobalt`, and cobalt_ext's no-caption path goes
`_download_audio → _whisper_transcribe` with no duration or size check
(cobalt_ext.py:129-135). The only ceiling is `COBALT_DURATION_LIMIT`
(3 hours) — cobalt's own, not a cost guard.

Two failure shapes for a long captionless video:

1. **Cost leak:** 3 h of audio ≈ $1.08 of Whisper per capture — 6× the
   intended cap.
2. **Guaranteed-failure retry loop (worse):** OpenAI's audio endpoint
   rejects uploads over 25 MB. A ~128 kbps mp3 crosses that around
   ~26 minutes, so most 30-min+ captionless videos fail at the Whisper
   call — *after* the full cobalt download, and **before the snapshot is
   saved** (extraction never completes). All 3 automatic retries
   re-download the entire audio, then the capture fails permanently.
   You pay 4× bandwidth + cobalt load for zero transcript.

**Fix (small):**
- Gate the audio path on duration: yt-dlp metadata (already fetched in
  parallel, cobalt_ext.py:129-132) carries `duration` — `_unpack_metadata`
  just doesn't surface it. When `duration > max_transcript_min * 60`,
  skip audio and emit the same "transcript skipped: duration exceeds cap"
  note ytdlp_ext produces (YouTube captures still get captions-first and
  the oEmbed fallback, so this only affects the no-caption tail).
- Belt-and-braces: after `_download_audio`, check file size against the
  25 MB API cap before uploading (covers the metadata-failed case where
  duration is unknown).

### N2 — Render failures are invisible (P1) — ✅ SHIPPED 2026-07-03

> Implemented on this branch (the recommended option): render exceptions
> now propagate — the worker marks the row failed with normal backoff,
> the retry reuses the persisted snapshot + classifier output (re-pays
> only the render), and permanent failures surface in
> `/captures?status=failed` with the real error.

`process_capture` catches any render exception, sets `rendered = None`,
appends a "Render failed — see server logs" callout, and **marks the
capture `done`** (orchestrator.py:188-190, 236). The row carries no
error, so nothing in `GET /captures` (or the extension history) can
distinguish these from healthy captures. During a Sonnet outage every
capture "succeeds" degraded, and finding them later means visually
scanning docs.

**Recommendation: let the render exception propagate.** This was the
right call *before* June's #2; now it's strictly better: on retry the
snapshot and classifier output are reused, so the retry re-pays only the
render call. Automatic backoff (60 s / 5 min / 30 min) rides out most
API blips; a longer outage lands the row in `failed` with the real error
recorded — visible in `/captures?status=failed`, retryable from the
extension. Trade-off: the doc stays a stub until a render succeeds
(today's behavior at least lands the raw transcript). If you value
transcript-lands-immediately more than failure visibility, the
alternative is keeping degraded-done but persisting the render error on
the row (the `error` column exists on `done` rows too) — one line, but
`status=failed` filtering won't find them.

### N3 — Tests are green but nothing runs them, and 5 tests aren't hermetic (P1) — ✅ SHIPPED 2026-07-03

> Implemented on this branch: `tests/conftest.py` autouse fixture strips
> proxy env vars (suite verified green both with and without them), and
> `.github/workflows/ci.yml` runs ingest pytest + mcp-ext typecheck +
> mcp-agent/browser-extension vitest — every job verified green locally
> before the workflow was added.

There is no CI (`.github/` doesn't exist). The suite is 433 tests in
under 5 seconds — ideal CI material — and this repo's development model
is Claude-authored PRs (#66–#72...), which are exactly the PRs that
benefit from an automatic gate.

Related: running the suite in any environment with `HTTP_PROXY`/
`HTTPS_PROXY` set fails 5 tests. `_fetch_sync` branches on proxy env
vars (_youtube_transcript.py:222-233) and passes `http_client=` to
`YouTubeTranscriptApi`, whose test fakes take no constructor args. The
production behavior is correct; the tests assume a clean env.

**Fix (small):**
- Autouse fixture (conftest.py) that monkeypatch-deletes the four proxy
  vars, making the suite environment-independent.
- One GitHub Actions workflow: `pytest` for `ingest/` on Python 3.12,
  plus `npm ci && npm test` / `tsc --noEmit` for `mcp-ext`, `mcp-agent`,
  and the browser extensions. All are fast; a single workflow file
  covers the whole repo.

### N4 — Unpinned dependencies make every image rebuild a gamble (P1) — ✅ SHIPPED 2026-07-03

> Implemented on this branch: `ingest/constraints.txt` (suite-validated
> pins, yt-dlp deliberately floating, refresh recipe in the header),
> wired into the Dockerfile and CI. A fresh venv installed with the
> constraints passes the full suite. Bonus catch: scenedetect 0.7 dropped
> the `[opencv]` extra pyproject requests — under the pin, opencv-python
> is a hard dependency of scenedetect, so `cv2` stays guaranteed.

`pyproject.toml` uses only `>=` constraints and the Dockerfile does
`pip install .` at build time — so every Portainer stack update resolves
the *latest* of anthropic, openai, markitdown (a `0.0.x` pre-release
package!), scenedetect, numpy, etc. The render pipeline leans on
SDK-specific behavior (`messages.parse`, `output_format=`,
`parsed_output`) that fast-moving SDKs have changed before. One breaking
upstream release = a broken stack redeploy at an unrelated moment, with
nothing in the diff to explain it.

**Fix (small):** generate a lock/constraints file (`uv pip compile` or
`pip freeze` from a known-good build) and `COPY` + `pip install -c` it
in the Dockerfile. Deliberate exception: let `yt-dlp` float (its whole
value is tracking the extractor arms race) — pin everything else.
Refresh the lock intentionally, in its own commit, so a dep bump is
bisectable from a code change.

### N5 — Rerender is still append-only (P2, carried over) — ✅ SHIPPED 2026-07-03

> Implemented on this branch (full-replace, per owner's choice): rerender
> now deletes every existing block and appends the fresh render via the
> shared `replace_doc_blocks` helper (tolerant of already-gone blocks;
> degrades to append when listing fails). Manual doc edits are removed
> from the live doc but recoverable via AFFiNE's per-doc history.
> Concurrent rerenders of the same capture are serialized in-process.

The last user-visible half of June #8. `POST /captures/{id}/rerender`
appends a second copy of every section (api.py:735-833, documented v1
caveat). Now that the extension ships a Templates tab, the natural loop
is *edit template → rerender → compare* — and every iteration duplicates
the doc body. The pieces already exist: `list_doc_blocks` +
`delete_block` are in the MCP client and the orchestrator already uses
them for stub cleanup. Replace = delete all existing blocks, then append
`build_doc_blocks` output. While there, add a per-capture in-process
lock (or a status guard) so two concurrent rerenders don't interleave.

### N6 — Cost accounting (P2, carried over) — ✅ SHIPPED 2026-07-03

> Implemented on this branch: `src/llm_usage.py` contextvar collector
> installed by the worker per capture; all 8 billable call sites record
> (classify / render / render_map / render_reduce / template_synth /
> vision / embedding / whisper-bytes). Aggregated summary persisted to
> `captures.cost_breakdown` (JSONB, migration 0007) on success AND
> failure, emitted as one structured log line, and exposed in
> `GET /captures/{id}`. Not covered: API-triggered `/rerender` runs
> without a collector (records are silent no-ops there).

Still nothing records spend. Every capture makes 2–19 LLM calls
(classify + render or map-reduce + optional vision) plus Whisper minutes
and an embedding call, and `response.usage` is discarded at every site.
With the shared clients in place (llm_clients.py) there's now a single
choke point: wrap `messages.parse`/`messages.create` to accumulate
`{model, input_tokens, output_tokens, cache_read, cache_write}` into a
per-capture context (contextvar, mirroring `set_capture_id`), persist as
a `cost_breakdown` JSONB column on `mark_done`/`mark_failed`, and emit
one structured log line per capture.

Why before more tuning: chunk thresholds, `max_chunks_per_capture`,
video-analysis ROI, and model choices are all guesses until you can
grep spend per platform/topic. It will also show whether the
`cache_control` markers are doing anything — the classifier system
prompt (~350 tokens) is below Haiku's minimum cacheable prefix, so some
of those markers are likely no-ops today.

### N7 — Hygiene (P3, opportunistic) — partially shipped 2026-07-03

> Shipped: cursor pagination on GET /captures (`cursor` param +
> `next_cursor`), explicit timeout/max_retries on the shared LLM clients,
> bearer-token auth on /health/deep + /diagnostic/logging (plain /health
> stays open for the Docker healthcheck), and the retry-endpoint
> `_row_to_response(row, None)` wart. Still open: topics.yaml hot-reload,
> api.py router split, `filer._mcp` reach-ins, per-query pool acquisition.

- **`GET /captures` pagination is stubbed** — `next_cursor` is always
  `null` (api.py:478) even though `list_captures` already supports
  `before` (db.py:297-299). History views silently cap at 200 items.
- **`llm_clients` lacks explicit `timeout`/`max_retries`** — the June
  proposal asked for them; SDK defaults (10 min, 2 retries) now govern
  16-way-parallel chunk calls inside a 30-min capture budget.
- **topics.yaml hot-reload** still deferred (api.py:77) — editing topic
  hints requires a stack redeploy.
- **api.py is 1012 lines** — split into routers (captures / templates /
  cookies / health) before it grows again.
- **`filer._mcp` reach-ins** persist across orchestrator + api (~8
  sites) — promote to a public attr or inject the client.
- **`_row_to_response(row, None)`** wart still at api.py:517.
- **`/health/deep` and `/diagnostic/logging` are unauthenticated** —
  fine for `/health` (Docker healthcheck needs it), but the deep probe
  reports internal topology and latencies on a port that defaults to
  `0.0.0.0`. Cheap to put behind `require_token`.
- **Per-query pool acquisition** (June #9 remainder, worker.py:153) —
  only matters if you raise `WORKER_CONCURRENCY` past ~4; with
  `max_size=8` each worker pins a connection for the whole capture
  while `/capture`, `/health`, and the filer each grab more.

---

## Recommended execution order

| Batch | Items | Why this order |
|---|---|---|
| 1 | N1 cobalt Whisper guard | Active cost leak + guaranteed-failure retry loop; smallest fix with the largest $ impact |
| 2 | N3 CI + test hermeticity, N4 dependency lock | Process safety net — makes every later batch (and every Claude PR) cheaper to verify; suite is already green and fast |
| 3 | N2 render-failure visibility, N6 cost accounting | Same theme (see what the pipeline actually does); cost data then drives tuning decisions |
| 4 | N5 rerender replace semantics | Completes the template-editing loop the extension UI already exposes |
| 5 | N7 hygiene picks | Opportunistic; do pagination + client timeouts when touching api.py anyway |

Deliberately **not** proposed: raising worker concurrency (single-user
share rate doesn't need it; do per-query pool acquisition first if you
ever do), LISTEN/NOTIFY (wake event covers it), and new capture sources
(design doc §18) until the cost/observability items above are in — new
sources multiply spend you currently can't measure.
