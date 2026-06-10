# Ingest Service — Improvement Proposal

**Status:** Proposal · **Date:** 2026-06-10 · **Scope:** `ingest/` only

A review of the ingest service as implemented (post Phase 16). The pipeline
works end-to-end; the items below are reliability gaps, cost leaks, and API
blind spots found by reading the current code, ordered by priority.

---

## How it works today (summary)

```
POST /capture (api.py:348)
  ├─ idempotency via url_hash lookup
  ├─ SYNC: resolve folder + create stub doc + move + append (4 MCP calls)
  └─ INSERT captures row (status=queued) → 202

Worker (worker.py) — single asyncio task, 2s poll
  └─ claim_next_queued / claim_due_failed (FOR UPDATE SKIP LOCKED)
      └─ orchestrator.process_capture (orchestrator.py:37)
           extract (cobalt/ytdlp/markitdown/... per topics.yaml)
             ├─ YouTube: captions-first → cobalt+Whisper fallback → oEmbed-only fallback
             └─ optional video analysis (download → scenedetect → quality filter → Sonnet vision → keyframe blobs)
           classify (Haiku, sibling folders + hints) → mark_classifying
           resolve/synthesize content template
           save extracted_snapshot
           render (Sonnet; chunked map-reduce > 12k chars)
           set title → move to topic folder (embedding dedup + aliases) → replace doc body
           mark_done
  Failure → mark_failed, backoff 60s/5min/30min, then permanent.
```

---

## P0 — Reliability

### 1. Worker dies permanently on a transient DB error
`Worker._loop` (worker.py:95) wraps only the *dispatch* in try/except. The
claim itself (`row = await self._claim_next()`) is unprotected — one
connection hiccup (Postgres restart, pool timeout) raises out of the `while`,
the task ends, and nothing restarts it. Worse, `/health` returns `ok: true`
regardless of `worker_alive` (api.py:240), and the compose healthcheck only
checks HTTP 200 — so Docker never restarts the container. Captures silently
queue forever.

**Fix:**
- Wrap the whole loop iteration in try/except with a short sleep on error.
- Make `/health` return 503 (or at least `ok: false`) when
  `worker_alive == false`, so the existing healthcheck restarts the container.

### 2. Retry re-pays extraction (Whisper + video analysis)
`process_capture` reuses cached classifier output on retry
(orchestrator.py:85) but **always re-extracts**. The `extracted_snapshot` is
saved only *after* classification (orchestrator.py:131). If render or filing
fails, the retry re-downloads audio, re-runs Whisper (~$0.006/min), re-runs
video download + Sonnet vision. Three retries of a failed filing step on a
30-min video ≈ 4× the extraction spend.

**Fix:** save the snapshot immediately after extraction, and at the top of the
pipeline reuse `row.extracted_snapshot` when present (mirroring the
classifier-reuse pattern). `api.py:_snapshot_to_extracted` already does the
deserialization.

### 3. Idempotency race on concurrent duplicate /capture
`/capture` does `get_by_url_hash` → (4 MCP calls) → `insert` (api.py:362-407).
Two concurrent posts of the same URL both miss the lookup; the second INSERT
hits the `url_hash` UNIQUE constraint → unhandled `UniqueViolationError` →
500, plus an **orphaned stub doc** already created in AFFiNE. The design doc
(§16) explicitly calls for "3× concurrently → exactly one doc".

**Fix:** catch `UniqueViolationError`, re-fetch by hash, return the existing
row, and best-effort `delete_doc` the orphan stub. (Full fix — reserve the row
first via `INSERT ... ON CONFLICT DO NOTHING RETURNING`, then create the doc —
can come later.)

### 4. No per-capture timeout
Nothing bounds `process_capture`. A hung subprocess or stalled stream blocks
the single worker indefinitely; with one worker that means the whole queue.
Most HTTP calls have timeouts, but the composition (download + Whisper +
vision + up to 17 Sonnet calls in chunked render) has no ceiling.

**Fix:** `asyncio.wait_for(process_fn(...), timeout=CAPTURE_TIMEOUT_SEC)` in
the worker (e.g. 30 min default, env-tunable). Timeout counts as a normal
failure → existing backoff applies.

---

## P1 — API correctness & resilience

### 5. `GET /captures/{id}` never returns `error` / `completed_at`
`_row_to_detail` hardcodes `error=None, completed_at=None` (api.py:1032-1033)
because `CaptureRow` / `_CAPTURE_COLS` (db.py:41) don't select those columns.
The iOS history view literally cannot show *why* a capture failed — the
operator has to grep server logs. The columns exist in the schema; this is
just plumbing.

**Fix:** add `error`, `completed_at`, `next_attempt_at` to `_CAPTURE_COLS` +
`CaptureRow`, surface them in `CaptureDetail`.

### 6. `queue_depth` counts permanently-failed rows
`count_active` (db.py:233) includes `failed` — even rows with
`next_attempt_at IS NULL` (permanent, never retried). One permanent failure
makes `queue_depth` never reach 0, which poisons it as a monitoring signal.

**Fix:** count `failed` only where `next_attempt_at IS NOT NULL`; optionally
report `{queued, in_flight, failed_retryable, failed_permanent}` separately.

### 7. `/capture` hard-fails when AFFiNE/mcp_ext is down
Stub creation is synchronous: if mcp_ext or AFFiNE is briefly down, the share
from iOS gets a 5xx and the capture is **lost** (user has to remember to
re-share). The DB is usually still up.

**Fix:** on MCP failure, still insert the row (status `queued`, `doc_id=NULL`)
and return 202 with `web_url=null`; teach the orchestrator to create the doc
when `row.doc_id is None`. Crash-recovery semantics already tolerate this
shape. (Also: pass the stub paragraph via `create_doc(initial_blocks=...)` —
the client supports it — saving one of the four sequential MCP round-trips on
the hot path.)

### 8. Rerender duplicates blocks and ~80 lines of orchestrator code
`POST /captures/{id}/rerender` appends a second copy of every section
(documented v1 caveat, api.py:719) and re-implements the block assembly that
lives in `orchestrator._replace_doc_body_templated` — the two have already
drifted slightly (no stub-deletion in the rerender copy).

**Fix:** extract one shared `build_doc_blocks(rendered, keyframes, extracted,
url)` helper; implement replace semantics with the existing
`list_doc_blocks` + `delete_block` tools (delete all blocks, then append).

---

## P2 — Throughput & cost

### 9. Single worker = head-of-line blocking
A 2-hour podcast (chunked render: up to 17 Sonnet calls) blocks every
subsequent capture for many minutes. The DB claim is already
`FOR UPDATE SKIP LOCKED`-safe, so concurrency is cheap to add:
run N worker coroutines (N=2–3, env `WORKER_CONCURRENCY`) sharing the same
claim logic. Related: the worker currently holds a pool connection for the
*entire* pipeline run (worker.py:117) even though the orchestrator only
touches the DB at step boundaries — acquire per-query (the repo already takes
a pool-or-conn duck type) before raising concurrency.

### 10. 2s poll → wake event
`POST /capture` inserts and waits for the next poll tick. An
`asyncio.Event` the API sets after insert (worker waits on
`event | timeout`) gives instant pickup with ~5 lines; Postgres
LISTEN/NOTIFY is overkill for one process.

### 11. SDK clients constructed per call
`AsyncAnthropic(...)` / `AsyncOpenAI(...)` are built inside every call at 7
sites (classifier.py:113, templated_render.py:115, chunked_render.py:446,
template_synth.py:139, video_analysis.py:370, ytdlp_ext.py:198,
embeddings.py:23). That defeats HTTP connection pooling and scatters
retry/timeout config. **Fix:** one lazy module-level client per SDK (e.g.
`src/llm_clients.py`), with explicit `timeout` + `max_retries`.

### 12. Classifier should use structured outputs
`classifier.py` hand-parses JSON (manual fence-stripping, `json.loads`,
classifier.py:134-141) while `templated_render` already uses
`messages.parse(output_format=...)`. Migrating removes a whole class of
parse failures → wasted retries of the full pipeline.

### 13. No cost accounting
Every capture spends: Whisper minutes, Haiku classify, Sonnet render (1–17
calls), Sonnet vision, OpenAI embeddings — and nothing records it. Anthropic
responses carry `usage`; Whisper cost is `duration`. **Fix:** accumulate a
`cost_breakdown` JSONB on the capture row (or at minimum one structured log
line per capture with token totals) so spend per platform/topic is greppable.

---

## P3 — Hygiene (worth doing opportunistically)

- **`topics.yaml` hot-reload** promised in the design doc (§6) but deferred
  (api.py:79) — also `lifespan` loads topics twice (api.py:79 and :159);
  a change to the file requires a container restart today.
- **`api.py` is 1052 lines** — split into routers (`captures`, `templates`,
  `cookies`, `health`) now that endpoint count has grown.
- **`filer._mcp` reached into from outside** (orchestrator, api) — promote to
  a public attribute or pass the MCP client explicitly.
- **`mark_failed` stores unbounded `str(exc)`** — truncate to ~2 KB; some
  httpx errors embed response bodies.
- **`_row_to_response(row, None)`** in the retry endpoint passes `None` for a
  typed parameter (api.py:471) — drop the unused `router` param instead.
- **Text-only idempotency** hashes `shared_text` through URL normalization
  (api.py:370) — works, but a dedicated `text_hash` path would be clearer.

---

## Suggested implementation order

| Batch | Items | Why together |
|---|---|---|
| 1 | #1, #4, #6 | Worker robustness — small, self-contained, biggest ops win |
| 2 | #2, #5 | Snapshot-first + error surfacing — touches db.py columns once |
| 3 | #3, #7 | /capture hardening — same endpoint |
| 4 | #8, #11, #12 | Refactors with test-only risk |
| 5 | #9, #10, #13 | Throughput + cost, after the worker is robust |
