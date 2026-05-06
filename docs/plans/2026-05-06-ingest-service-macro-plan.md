# AFFiNE Ingest Service — Macro Implementation Plan

> **For agentic workers:** This is a **macro plan** — it sequences phases, each of which will receive its own detailed task-level plan via the `writing-plans` skill before execution. Use `superpowers:subagent-driven-development` to execute each phase.

**Goal:** Ship a Python/FastAPI ingest service that captures URLs from a paired iOS app, extracts content via yt-dlp + markitdown (+ Whisper API fallback), classifies via Anthropic Haiku 4.5, and files into AFFiNE's `Sources/` tree with hierarchical folder organization.

**Spec:** [`docs/specs/2026-05-06-ingest-service-design.md`](../specs/2026-05-06-ingest-service-design.md)

**Architecture:** Single Python container (`ingest`) sibling to existing `mcp_ext` and `mcp_agent`. HTTP API + asyncio worker share one process. Postgres new database `affine_ingest` on existing pgvector instance. All AFFiNE writes go through `mcp_ext` HTTP MCP. All inference (LLM, transcription) hits hosted APIs.

**Tech Stack:**
- Python 3.12 · FastAPI · uvicorn · asyncpg · pydantic v2 · pytest + pytest-asyncio
- yt-dlp · markitdown (Microsoft) · ffmpeg
- Anthropic Python SDK (Claude Haiku 4.5 for classify, Sonnet 4.6 for reorg)
- OpenAI Python SDK (Whisper transcription, text-embedding-3-small)
- TypeScript / node-cron (existing `mcp-agent`, extended with one automation)

**Total effort estimate:** ~3–5 days of focused work for a developer. Each phase is independently shippable; service is usable end-to-end after Phase 6.

---

## Phase Dependency Graph

```
Phase 1 (compose + DB) ──► Phase 2 (MCP client + filer) ──► Phase 3 (stub + idempotency)
                                                                     │
                                                                     ▼
                                                      Phase 4 (extractors) ──► Phase 5 (classify + file)
                                                                                        │
                                                                                        ▼
                                                                            Phase 6 (worker loop + retry)
                                                                                        │
                                                                                        ▼
                                                                               Phase 7 (read + manage API)
                                                                                        │
                                                       ┌────────────────────────────────┤
                                                       ▼                                ▼
                                            Phase 8 (reorganizer)               Phase 9 (hardening + docs)
                                            (independent of 7)
```

Phases 1–3 are strictly sequential. Phases 4–7 build a vertical slice each and unlock end-to-end usability at Phase 6. Phase 8 (reorganizer) is independent — it lives in `affine-mcp-agent`, can be done in parallel after Phase 5. Phase 9 closes out.

---

## Phase 1 — Compose foundation + DB migration job

**Goal:** Bring up an empty `ingest` container with health endpoint responding, `affine_ingest` database created with schema applied, ollama removed.

**Files:**
- Modify: `compose.yaml` (remove `ollama`, `ollama_preload`, `ollama_data` volume; add `ingest` + `ingest_migration` services)
- Modify: `.env.example` (add `INGEST_API_TOKEN`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `INGEST_PORT`, `MAX_TRANSCRIPT_MIN`, `REORG_THRESHOLD_DEFAULT`; remove `OLLAMA_*`)
- Create: `ingest/Dockerfile` (python:3.12-slim + ffmpeg + uv install)
- Create: `ingest/pyproject.toml` (deps + dev deps + entry points)
- Create: `ingest/src/__init__.py`, `ingest/src/api.py` (FastAPI app with `/health` only), `ingest/src/migrate.py`, `ingest/src/config.py`
- Create: `ingest/migrations/0001_init.sql` (full schema from spec §10)
- Create: `ingest/tests/test_health.py`

**Acceptance:**
- `docker compose build ingest ingest_migration` succeeds.
- `docker compose up -d` brings up `affine_ingest` container with status `healthy`.
- `docker exec affine_postgres psql -U $DB_USERNAME -l` shows the `affine_ingest` database.
- `docker exec affine_postgres psql -U $DB_USERNAME -d affine_ingest -c '\d captures'` shows the table with all columns and indexes.
- `curl http://localhost:3200/health` returns `{"ok": true, "queue_depth": 0, "worker_alive": false, "version": "0.1.0"}`.
- `docker compose ps` does NOT list `affine_ollama` or `affine_ollama_preload`.

**Out of scope for this phase:** Authentication on `/health`. Worker process. Any business endpoint.

---

## Phase 2 — MCP client + AFFiNE write path (`filer`)

**Goal:** From within the ingest container, programmatically create a folder path under `Sources/`, create an empty doc in it, append a block, and move it. All via the existing `mcp_ext:3100` HTTP MCP.

**Files:**
- Create: `ingest/src/mcp_client.py` (async HTTP client for MCP protocol against `mcp_ext`; methods: `list_folder_tree`, `find_doc_by_title`, `create_folder`, `create_doc`, `append_blocks`, `move_document`, `delete_doc`)
- Create: `ingest/src/pipeline/__init__.py`
- Create: `ingest/src/pipeline/filer.py` (`resolve_or_create_folder(path: list[str]) -> folder_id`, `file_doc(folder_path, title, body_md, meta) -> doc_id`)
- Create: `ingest/tests/test_mcp_client.py` (mocked transport tests)
- Create: `ingest/tests/test_filer_integration.py` (gated by `INTEGRATION=1`, hits a running stack)

**Acceptance:**
- Unit tests pass with mocked MCP responses for each method.
- Integration test (gated): with the full stack up and a fresh test workspace, `filer.file_doc(["Sources","Socials","Instagram","Recipes"], "Test recipe", "# hi", {})` produces a doc in AFFiNE under that exact path. Verified by re-reading `list_folder_tree` and finding the doc.
- Re-running the same call resolves to the existing folders without creating duplicates (idempotent `resolve_or_create_folder`).

**Out of scope:** Embedding similarity. Authentication negotiation (use existing `AFFINE_ACCESS_TOKEN`).

---

## Phase 3 — Platform router + `POST /capture` stub creation + idempotency

**Goal:** iOS share extension can hit `POST /capture` with a URL and get back a 202 with a `web_url` to a stub doc in the right `Sources/<group>/<platform>` folder, in <500 ms p50. Same URL twice returns the existing capture.

**Files:**
- Create: `ingest/topics.yaml` (initial platforms map from spec §6 + topic_hints stub)
- Create: `ingest/src/pipeline/router.py` (`detect_platform(url) -> Platform`)
- Create: `ingest/src/db.py` (asyncpg pool + queries: `insert_capture`, `get_capture_by_url_hash`, `get_capture_by_id`, `update_capture_status`)
- Create: `ingest/src/models.py` (Pydantic request/response models)
- Modify: `ingest/src/api.py` (add auth dependency, `POST /capture` route)
- Create: `ingest/tests/test_router.py` (30+ URL routing assertions)
- Create: `ingest/tests/test_capture_endpoint.py` (FastAPI TestClient + mocked filer)

**Acceptance:**
- 30+ URL → platform routing assertions pass (YouTube variants, Instagram variants, X/Twitter, TikTok, arXiv, Reddit, generic article).
- `POST /capture` with no Authorization → 401.
- `POST /capture` with valid token + Instagram URL → 202 with `web_url`, `doc_id`, `platform: "instagram"`, `initial_path: "Sources/Socials/Instagram"`. Total response time <500 ms p50 measured with `pytest-benchmark`.
- Submitting the same normalized URL again → 202 with the same `capture_id` and `doc_id`, no new DB row, no new doc in AFFiNE.
- Stub doc visible in AFFiNE with title from `shared_title` (or URL fallback) and body `> Capturing... (<timestamp>)`.

**Out of scope:** Any extraction. Worker queue (just insert with status `queued`, no work happens yet).

---

## Phase 4 — Extractor registry + per-platform extractors

**Goal:** Given a URL, the extraction layer returns a normalized `Extracted` record (title, body_md, author, published_at, media_kind) for every platform in `topics.yaml`.

**Files:**
- Create: `ingest/src/pipeline/extracted.py` (`Extracted` dataclass)
- Create: `ingest/src/pipeline/extractors/__init__.py` (registry: `get_extractor(platform_id) -> Extractor`)
- Create: `ingest/src/pipeline/extractors/markitdown_ext.py`
- Create: `ingest/src/pipeline/extractors/ytdlp_ext.py` (info-json + auto-subs; Whisper fallback if duration ≤ `MAX_TRANSCRIPT_MIN` and no captions; otherwise metadata-only with note)
- Create: `ingest/src/pipeline/extractors/oembed_ytdlp_ext.py` (X/Twitter)
- Create: `ingest/src/pipeline/extractors/reddit_json_ext.py`
- Create: `ingest/tests/test_extractors_unit.py` (mocked subprocess + mocked HTTP for each)
- Create: `ingest/tests/fixtures/` (sample yt-dlp info-json files, sample reddit JSON, sample HTML for markitdown)
- Create: `ingest/tests/test_extractors_integration.py` (gated by `INTEGRATION=1`, hits real URLs — keep set small + stable)

**Acceptance:**
- Each extractor's unit tests pass.
- `MAX_TRANSCRIPT_MIN` cap honored: a 90-min YouTube URL with no captions → `body_md` contains a "transcript skipped" note, NOT a Whisper API call.
- Temp files in `/tmp/ingest` are deleted after extraction success and after extraction failure (verify with `os.listdir`).
- `body_md` is truncated to 50,000 chars max.
- Integration test against a known stable arxiv URL + a known YouTube URL (with captions) passes when `INTEGRATION=1`.

**Out of scope:** Filing the result. Classification. (Extractor returns its record; nothing consumes it yet.)

---

## Phase 5 — Classifier + folder embedding similarity + filing

**Goal:** Given an `Extracted` record, the classifier picks a topic (`{topic, confidence, reasoning, alias_of}`) using sibling context from `list_folder_tree` + `topics.yaml` hints. Embedding similarity prevents folder duplication (Recipes vs Cooking). The filer then ensures the topic folder exists and moves the doc + appends the body.

**Files:**
- Create: `ingest/src/pipeline/classifier.py` (Anthropic call with prompt caching on the system prompt, returns validated `ClassificationResult`)
- Create: `ingest/src/pipeline/embeddings.py` (OpenAI embedding call, cosine similarity, persist in `folder_embeddings` table, lookup by `parent_path`)
- Modify: `ingest/src/pipeline/filer.py` (add `move_to_topic_folder`, integrate similarity check before `create_folder`)
- Modify: `ingest/src/db.py` (add `folder_embeddings` and `topic_aliases` queries)
- Create: `ingest/tests/test_classifier.py` (mocked Anthropic responses, prompt assembly golden file)
- Create: `ingest/tests/test_embeddings.py` (synthetic embeddings, alias collapse cases)

**Acceptance:**
- Classifier prompt assembly matches a golden file for known input.
- Mocked Anthropic response with `{topic: "Recipes", confidence: 0.92}` → filer moves doc to `Sources/Socials/Instagram/Recipes/` (folder created if missing).
- Mocked Anthropic response with `{topic: "Cooking", confidence: 0.9}` AND existing `Recipes` folder with embedding cosine 0.91 → doc moves to existing `Recipes/`, no `Cooking/` folder created. `topic_aliases` row inserted.
- Mocked Anthropic response with `confidence: 0.4` → doc stays at `Sources/Socials/Instagram/`, `needs_classification = TRUE` set in DB.
- Real Anthropic integration test (gated, single call): submit a real Instagram recipe → `topic: "Recipes"` returned with `confidence > 0.8`.

**Out of scope:** Worker loop wiring. Retry. (Functions are testable from pytest, not yet from `POST /capture`.)

---

## Phase 6 — Worker loop + state machine + retry + crash recovery

**Goal:** Submit `POST /capture` → service automatically extracts, classifies, files. Capture row transitions through `queued → extracting → classifying → filing → done`. Failures retry with `60s, 5min, 30min` backoff. Container restart resumes in-flight items. End-to-end pipeline works.

**Files:**
- Create: `ingest/src/worker.py` (asyncio task loop: poll `captures` for `queued` or `failed AND next_attempt_at <= now()`, run pipeline per item, update status atomically)
- Modify: `ingest/src/api.py` (start worker on app startup with `lifespan` handler; expose `worker_alive` in `/health`)
- Modify: `ingest/src/db.py` (add `claim_next_capture` SELECT FOR UPDATE SKIP LOCKED query, status transition queries)
- Create: `ingest/tests/test_worker.py` (asyncio test of full state machine with fakes for extractors, classifier, filer)
- Create: `ingest/tests/test_crash_recovery.py` (insert capture in `extracting` state, start worker, expect resume)

**Acceptance:**
- `POST /capture` with a real URL (one mock per integration test, fakes inside unit tests) → within 60 s, `GET /captures/{id}` returns `status: "done"` with `topic_path` filled.
- Inducing a failure in the extractor (raise) → status transitions to `failed`, `retry_count = 1`, `next_attempt_at` is ~60 s in the future. After 60 s the worker retries automatically. After 3 retries: stuck at `failed`.
- Hard-killing the container during `extracting` (with a slow fake extractor) → on restart, the worker logs "resuming capture <id> from extracting" and finishes the pipeline. No duplicate doc created.
- `GET /health` returns `worker_alive: true` and `queue_depth: N` matching DB.

**Out of scope:** Read endpoints (list, get, retry, delete) — Phase 7.

---

## Phase 7 — Read + manage endpoints

**Goal:** Full HTTP API per spec §4 — `GET /captures`, `GET /captures/{id}`, `POST /captures/{id}/retry`, `DELETE /captures/{id}`. iOS app can list history and manage captures.

**Files:**
- Modify: `ingest/src/api.py` (add 4 routes)
- Modify: `ingest/src/db.py` (list query with filters + cursor pagination, single get, retry update, soft-delete)
- Modify: `ingest/src/mcp_client.py` (already has `delete_doc` from Phase 2, used by DELETE)
- Create: `ingest/tests/test_read_endpoints.py`
- Create: `ingest/tests/test_manage_endpoints.py`

**Acceptance:**
- `GET /captures?limit=10` returns at most 10 newest items, descending by `created_at`.
- `GET /captures?status=failed` returns only failed items.
- `GET /captures?platform=instagram&limit=50` filter combination works.
- `POST /captures/{id}/retry` on a `done` capture → re-queues it (status back to `queued`, `retry_count` reset for fresh re-classify), pipeline re-runs, body in AFFiNE doc is **replaced** (not appended).
- `POST /captures/{id}/retry` on a `queued` capture → 409 Conflict.
- `DELETE /captures/{id}` → soft-trash AFFiNE doc, capture status = `deleted`. Subsequent `GET` returns 404.
- All endpoints reject requests without `Authorization: Bearer ${INGEST_API_TOKEN}`.

**Out of scope:** OpenAPI spec generation (FastAPI gives this for free at `/docs` — no extra work).

---

## Phase 8 — Reorganizer extension to `mcp-agent`

**Goal:** Weekly automation that scans `Sources/` leaf folders, splits any with >`REORG_THRESHOLD` docs into 2–5 named sub-clusters via Claude Sonnet, executes the splits via existing `mcp-ext` write tools, logs the run.

**Files:**
- Create: `affine-mcp-agent/src/automations/sources-reorg.ts`
- Modify: `affine-mcp-agent/src/scheduler.ts` (register the cron entry: `0 3 * * 0` UTC → `sources-reorg`)
- Create: `affine-mcp-agent/src/lib/anthropic.ts` (thin Sonnet client if not already present)
- Modify: `portainer-stack/.env.example` (add `ANTHROPIC_API_KEY` if not added in Phase 1; reuse same key)
- Create: `affine-mcp-agent/src/automations/__tests__/sources-reorg.test.ts` (mocked MCP + mocked Anthropic)
- Modify: `portainer-stack/prepare.sh` (verify it still picks up the new files — should automatically)

**Acceptance:**
- Unit test: with a synthetic folder tree where `Sources/Socials/Instagram/Recipes` has 20 fake docs, mocked Anthropic returns a 3-cluster split → automation calls `create_folder` 3× and `move_document` 20× with the correct mapping.
- `docker exec affine_mcp_agent npx tsx src/automations/sources-reorg.ts` runs against the live workspace as a manual smoke test, no errors when no folder exceeds threshold (no-op exit).
- After execution, an entry exists in `Sources/Operations/Logs/reorganizer-YYYY-MM-DD.md` describing what was split.
- Scheduler registers the new cron entry on container restart.

**Out of scope:** Re-classifying individual docs (the reorganizer splits, doesn't reclassify). Cross-platform topic merging.

---

## Phase 9 — Hardening, docs, end-to-end smoke

**Goal:** Production-ready posture: structured logging, cost guards verified, README updated, end-to-end smoke from a real share-sheet-equivalent request.

**Files:**
- Modify: `portainer-stack/README.md` (add ingest section: env vars, bring-up, smoke test, troubleshooting)
- Create: `ingest/src/logging_setup.py` (structured JSON logging, `capture_id` correlation)
- Modify: `ingest/src/api.py`, `ingest/src/worker.py` (use structured logger)
- Create: `ingest/tests/test_cost_guards.py` (assert `MAX_TRANSCRIPT_MIN` and `MAX_BODY_CHARS` paths)
- Create: `ingest/scripts/smoke.sh` (curl against running stack; tests YouTube + arXiv + Reddit URLs end-to-end)

**Acceptance:**
- Smoke script run against the deployed stack: each of 3 URL types → doc visible in AFFiNE under the expected `Sources/<group>/<platform>/<topic>` path within 60 s.
- `docker logs affine_ingest --since 10m | jq .` produces valid JSON for every line; each line has `capture_id` correlation key when applicable.
- README includes: setup checklist, sample `.env`, how to generate `INGEST_API_TOKEN`, how to test from `curl`, how to view captures in DB, troubleshooting "why didn't my capture work" decision tree.
- Cost guard tests pass.
- All previously-deferred sanity items resolved or explicitly punted to v2 backlog.

**Out of scope:** APNs, file uploads, web admin UI, multi-user — all v2 per spec §17.

---

## Spec coverage map (self-review)

| Spec section | Phase covering it |
|---|---|
| §1 Goals & non-goals | Implicit — every phase serves a goal; non-goals never appear as tasks |
| §2 Architecture | Phase 1 |
| §3 Components | Phase 1 (ingest), Phase 2 (mcp-ext usage), Phase 8 (mcp-agent extension) |
| §4 HTTP API | Phase 3 (POST /capture), Phase 7 (rest), Phase 1 (/health) |
| §5 Pipeline state machine | Phase 6 |
| §6 Platform router | Phase 3 |
| §7 Extraction | Phase 4 |
| §8 Classification | Phase 5 |
| §9 Reorganizer | Phase 8 |
| §10 DB schema | Phase 1 (init), Phase 5 (folder_embeddings, topic_aliases — already in 0001 init) |
| §11 Repo layout | Phase 1 (scaffold), updated as phases land |
| §12 Compose changes | Phase 1 |
| §13 Auth & secrets | Phase 1 (.env), Phase 3 (auth dep) |
| §14 Idempotency & retry | Phase 3 (idempotency), Phase 6 (retry + crash recovery), Phase 7 (manual retry) |
| §15 Logging | Phase 9 |
| §16 Testing | Each phase ships its own tests; smoke in Phase 9 |
| §17 Out of scope (v1) | Honored everywhere — no phase sneaks these in |
| §18 Future work (v2+) | Tracked, not implemented |

No gaps. No placeholders. Type/method names consistent across phases (`Extracted`, `ClassificationResult`, `resolve_or_create_folder`, etc.).

---

## How to execute

This macro plan is the index. Each phase needs a detailed task-level implementation plan generated via `superpowers:writing-plans` **at the time you start that phase** (not all up-front — the spec evolves as you learn things in earlier phases, and a 9-phase task-level plan written now would be partially stale).

Recommended workflow per phase:

1. Read the phase block in this macro plan.
2. Invoke `writing-plans` with the spec section reference + this phase's goal/files/acceptance.
3. Get a step-by-step task plan saved to `docs/plans/2026-MM-DD-phase-N-<slug>.md`.
4. Execute via `superpowers:subagent-driven-development` (one subagent per task with checkpoints) — recommended for autonomous mode.
5. After phase acceptance criteria pass, commit, move to next phase.
