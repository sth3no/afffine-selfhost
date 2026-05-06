# AFFiNE Ingest Service — Design

**Status:** Draft v1 · **Date:** 2026-05-06 · **Repo:** `sth3no/afffine-selfhost`

A service that accepts arbitrary URLs/text from a paired iOS app, extracts the
underlying content, classifies it, and files it into the AFFiNE workspace under
the `Sources/` tree. Sibling to the existing `mcp-ext` (write proxy) and
`mcp-agent` (cron scheduler) in the same Portainer stack.

---

## 1. Goals & non-goals

### Goals
- Single endpoint (`POST /capture`) that accepts a URL and lands a
  fully-populated, well-classified document in AFFiNE without user follow-up.
- Cover the long tail of URL platforms: video sites (YouTube, Instagram,
  TikTok, X), articles, PDFs, podcasts, social posts, generic web pages.
- Hierarchical folder structure: `Sources/<group>/<platform>/<topic>/...` that
  evolves automatically as content accumulates.
- Survive iOS share-extension timeouts: 202-and-process pattern, no operation
  blocks the iOS UI for more than 500 ms.
- Run on a CPU-only VPS. All GPU-bound tasks (Whisper transcription, LLM
  classification) call hosted APIs.

### Non-goals (v1)
- APNs push notifications when processing completes.
- Multi-user auth — this is a single-user selfhost.
- File uploads from the iOS share sheet (only URLs and shared text).
- Web admin UI — observability is via Portainer logs and the iOS history view.
- Re-classifying existing documents when the topic whitelist changes.

---

## 2. Architecture

```
[iOS Share Ext]   [iOS Full App]
       │                │
       └────HTTPS───────┘
                │
                ▼
       ingest_service:3200             (Python 3.12, FastAPI + asyncio worker, 1 container)
       │       │       │
       │       │       └──► Anthropic API     (Claude Haiku 4.5 — classification)
       │       └────────► OpenAI Whisper API  (audio transcription fallback)
       └──HTTP MCP───► mcp_ext:3100  ──► affine:3010
                       (existing)         (existing)

Postgres (existing pgvector instance) — new database `affine_ingest`.
```

The HTTP server and the background worker share a single asyncio event loop in
the same container. CPU-bound work (yt-dlp, markitdown, ffmpeg) runs in a
threadpool via `asyncio.to_thread`. Queue depth is naturally bounded by the
share-rate of one human; no Redis/Celery is justified.

---

## 3. Components

| Component | Status | Role |
|---|---|---|
| `ingest_service` (Python) | **NEW** | HTTP API + extraction worker; this spec. |
| `mcp_ext` (TS) | existing | All AFFiNE writes — `create_doc`, `move_document`, `append_blocks`, `create_folder`, `list_folder_tree`, `find_doc_by_title`. |
| `mcp_agent` (TS) | existing — extended | Adds `sources-reorg.ts` automation for hierarchical folder splitting. |
| `affine`, `postgres`, `redis`, `manticoresearch` | existing | Untouched. |
| `ollama`, `ollama_preload` | **REMOVED** | All inference moves to hosted APIs. |

---

## 4. HTTP API

All endpoints require `Authorization: Bearer ${INGEST_API_TOKEN}` (single
shared token, rotated by editing the stack env in Portainer).

### `POST /capture`
Submit a URL or text snippet for ingestion.

**Request**
```json
{
  "url": "https://www.instagram.com/p/Cxyz123/",
  "source_app": "Instagram",          // optional; populated by iOS share ext
  "shared_title": "Honey-glazed salmon", // optional
  "shared_text": "..."                // optional; raw text shared without URL
}
```

At least one of `url` or `shared_text` must be present.

**Response — 202 Accepted**
```json
{
  "capture_id": "01J9X4M5...",
  "doc_id": "f3a85f64-5717-...",
  "web_url": "https://affine.example.com/workspace/<wsid>/f3a85f64-5717-...",
  "status": "queued",
  "platform": "instagram",
  "initial_path": "Sources/Socials/Instagram"
}
```

`web_url` is openable immediately (the stub doc exists). The body fills in as
the worker runs.

**Idempotency:** same normalized URL submitted twice returns the existing
capture row (status whatever it is now), does not enqueue work. URL
normalization: lowercase host, strip `utm_*` query params, strip trailing `/`.
Override with `POST /captures/{id}/retry`.

### `GET /captures?limit=50&status=&platform=`
Returns up to `limit` recent captures (newest first), filterable.

```json
{
  "items": [
    {
      "capture_id": "...",
      "url": "...",
      "platform": "instagram",
      "status": "done",
      "doc_id": "...",
      "web_url": "...",
      "topic_path": "Sources/Socials/Instagram/Recipes",
      "created_at": "2026-05-06T14:20:00Z",
      "completed_at": "2026-05-06T14:20:18Z"
    }
  ],
  "next_cursor": null
}
```

### `GET /captures/{capture_id}`
Single capture detail, including `error` (string), `retry_count`, last
`reasoning` from classifier.

### `POST /captures/{capture_id}/retry`
If `status in {failed, done}`, re-enqueue extraction. Use case: (1) failed
capture user wants to try again; (2) topic whitelist updated, user wants
re-classification of a specific item.

### `DELETE /captures/{capture_id}`
Soft-trash the AFFiNE doc via `delete_doc` and set capture status to `deleted`.

### `GET /health`
```json
{ "ok": true, "queue_depth": 2, "worker_alive": true, "version": "0.1.0" }
```

---

## 5. Pipeline state machine

```
            ┌─────────┐
POST ───►  │ queued  │──┐
            └─────────┘  │
                         ▼
                  ┌─────────────┐
                  │ extracting  │── fail ──►──┐
                  └─────────────┘             │
                         │                    │
                         ▼                    │
                  ┌─────────────┐             │
                  │ classifying │── fail ──►──┤
                  └─────────────┘             │
                         │                    │
                         ▼                    │
                  ┌─────────────┐             │
                  │   filing    │── fail ──►──┤
                  └─────────────┘             │
                         │                    ▼
                         ▼              ┌──────────┐
                  ┌─────────────┐       │  failed  │
                  │    done     │       └──────────┘
                  └─────────────┘
```

**Retry policy:** 3 retries with backoff `60s, 5min, 30min`. After exhaustion,
status = `failed`, error stored, manual `POST /retry` is the only escape.

**Stub creation (synchronous, before 202)**
1. Auth check.
2. Normalize URL, hash, look up by hash → return existing if found.
3. Detect platform from URL host (regex table; see §6).
4. Resolve initial folder path `Sources/<group>/<platform>` via cached
   `list_folder_tree`. Create missing intermediates with `create_folder`.
5. `create_doc` with placeholder title (`shared_title` if present, else URL),
   stub body `> Capturing... (2026-05-06 14:20)`.
6. INSERT row in `captures` table with status `queued`.
7. Schedule asyncio task. Return 202.

Step 1–7 must complete in <500 ms p50.

---

## 6. Platform router

URL-host → platform identity. Used to (a) pick initial folder, (b) route to
the correct extractor, (c) feed sibling-context to the classifier.

```yaml
# topics.yaml — platform routing
platforms:
  - id: youtube
    group: Socials
    folder_name: Youtube
    hosts: [youtube.com, www.youtube.com, youtu.be, m.youtube.com]
    extractor: ytdlp
  - id: instagram
    group: Socials
    folder_name: Instagram
    hosts: [instagram.com, www.instagram.com]
    extractor: ytdlp           # ytdlp does insta reels/posts
  - id: tiktok
    group: Socials
    folder_name: TikTok
    hosts: [tiktok.com, www.tiktok.com, vm.tiktok.com]
    extractor: ytdlp
  - id: x
    group: Socials
    folder_name: X
    hosts: [x.com, twitter.com, www.x.com]
    extractor: oembed_ytdlp    # oEmbed first, ytdlp for video tweets
  - id: reddit
    group: Socials
    folder_name: Reddit
    hosts: [reddit.com, www.reddit.com, old.reddit.com]
    extractor: reddit_json
  - id: podcast_apple
    group: Podcasts
    folder_name: Apple Podcasts
    hosts: [podcasts.apple.com]
    extractor: markitdown
  - id: arxiv
    group: Research papers
    folder_name: arXiv
    hosts: [arxiv.org]
    extractor: markitdown
  # ...catch-all:
  - id: article
    group: Articles
    folder_name: Web
    hosts: ["*"]
    extractor: markitdown
```

`group` and `folder_name` map to the existing `Sources/` taxonomy: `Sources/Socials`, `Sources/Articles`, `Sources/Podcasts`, `Sources/Research papers`, `Sources/Books`, `Sources/Conversations`, `Sources/Docs`, `Sources/Websites`.

Hot-reload: service watches `topics.yaml` mtime and reloads on change without
restart.

---

## 7. Extraction

Each extractor returns a normalized record:
```python
@dataclass
class Extracted:
    title: str | None
    body_md: str               # cleaned markdown, max 50_000 chars
    author: str | None
    published_at: datetime | None
    media_kind: Literal["text", "video", "audio", "image", "mixed"]
    extra: dict                # platform-specific extras (channel, hashtags, ...)
```

### `ytdlp`
1. `yt-dlp --skip-download --write-info-json --write-auto-sub --sub-lang
   en,cs --convert-subs vtt <url>` → JSON metadata + caption file (when
   available).
2. If no caption AND `duration <= MAX_TRANSCRIPT_MIN * 60`: `yt-dlp -x
   --audio-format m4a` → file → POST to OpenAI Whisper API → delete file.
3. If no caption AND duration too long: skip transcript, body contains
   metadata + a one-line note.
4. Pass everything through `markitdown` to normalize to MD.

### `markitdown`
- URL → `MarkItDown.convert(url)`. Microsoft's library handles HTML→MD with
  cleanup, PDFs, Office files, images (OCR), audio. For URLs it fetches with
  a normal `requests` UA.

### `oembed_ytdlp` (X / Twitter)
- Try `https://publish.twitter.com/oembed?url=...` first → text content.
- If embed has `video` field, also run `ytdlp` for the video.

### `reddit_json`
- Append `.json` to URL, fetch with a UA. Extract post title, body, top 5
  comments. Render as MD.

### Cost & resource guards
- `MAX_TRANSCRIPT_MIN=30` — caps Whisper API spend per item. Configurable in
  `.env`.
- `MAX_BODY_CHARS=50_000` — truncates aggressive markdown (long PDFs).
- Temp files in `/tmp/ingest`, deleted on extraction success or failure.
- `tmpfs` mount of 2GB limits worst-case disk pressure.

---

## 8. Classification

**Model:** `claude-haiku-4-5-20251001` via Anthropic SDK with prompt caching
on the system prompt.

**Inputs to the classifier:**
1. Platform (`instagram`).
2. Existing topic siblings under `Sources/Socials/Instagram/`, queried via
   `list_folder_tree` and cached for 60s.
3. Whitelist hints from `topics.yaml`:
   ```yaml
   topic_hints:
     instagram: [Recipes, Workouts, Travel, Architecture, Memes, Fashion]
     youtube: [Tutorials, Talks, Productivity, Programming, Music, Documentary]
     # ...
   ```
4. The `Extracted` record (title, body_md truncated to 8k chars, author).

**Output (JSON, validated):**
```json
{
  "topic": "Recipes",
  "confidence": 0.92,
  "reasoning": "Caption lists ingredients and step-by-step instructions; image shows plated dish.",
  "alias_of": null
}
```

**Sibling-aware prompt (excerpt):**
> Existing topic folders under `Sources/Socials/Instagram/`: **Recipes,
> Workouts, Travel**. Reuse one of these if the content fits. Only propose
> a new topic name if clearly distinct. If you propose a new topic that is
> semantically similar to an existing one (e.g., "Cooking" when "Recipes"
> exists), set `alias_of: "Recipes"` instead of creating a duplicate.

**Embedding-similarity safety net:** before creating a new folder, embed the
proposed name (`text-embedding-3-small`, OpenAI) and compare against
embeddings of existing siblings. If `cosine > 0.85`, alias to existing
(equivalent to `alias_of` set explicitly). Embeddings are persisted in
`folder_embeddings` table to avoid recompute.

**Confidence handling:**
- `confidence >= 0.6` → file in `Sources/<group>/<platform>/<topic>/`
- `confidence < 0.6` → file in `Sources/<group>/<platform>/` root, mark
  `needs_classification = true`. Reorganizer (§9) revisits these on its run.

---

## 9. Reorganizer (extends `mcp-agent`)

New file: `affine-mcp-agent/src/automations/sources-reorg.ts`.
Schedule (in `scheduler.ts`): weekly, Sunday 03:00 UTC.

**Algorithm**
1. `list_folder_tree(rootPath: "Sources")` → all leaf folders.
2. For each leaf folder where `doc_count > REORG_THRESHOLD` (default 15):
   a. `list_documents(folderId)` → titles + first 200 chars of each body.
   b. Anthropic Sonnet 4.6 call (Sonnet, not Haiku — splits matter):
      *"These N documents share the topic '<folder name>'. Propose 2–5
      intuitive sub-clusters with names. Return JSON: `[{name, doc_ids}]`.
      Each cluster needs ≥3 docs; smaller clusters stay in the parent."*
   c. For each proposed cluster:
      - `create_folder(parentFolderId, name)`
      - `move_document(docId, newFolderId)` for each `doc_id`.
   d. Append a dated entry to `Sources/Operations/Logs/reorganizer-YYYY-MM-DD.md`
      with what was split and why.
3. Idempotent: if no folder exceeds threshold, no-op. If a sub-folder is
   already named the same as a proposed cluster, reuse.

**Why Sonnet for reorg, Haiku for ingest classify?** Reorg runs ≤1×/week
across all folders simultaneously, decision quality matters; ingest runs
per-capture and latency/cost matter.

`REORG_THRESHOLD` and per-platform overrides live in `topics.yaml`:
```yaml
reorg:
  default_threshold: 15
  overrides:
    Sources/Socials/Instagram/Recipes: 25  # let recipes accumulate before splitting
```

---

## 10. Database schema (`affine_ingest` on existing postgres)

```sql
CREATE TABLE captures (
    id              TEXT PRIMARY KEY,            -- ULID
    url             TEXT,
    url_hash        TEXT UNIQUE,                 -- sha256(normalized_url)
    source_app      TEXT,
    shared_title    TEXT,
    shared_text     TEXT,
    platform        TEXT NOT NULL,
    status          TEXT NOT NULL,               -- queued|extracting|classifying|filing|done|failed|deleted
    doc_id          TEXT,
    web_url         TEXT,
    topic_path      TEXT,
    classifier_topic     TEXT,
    classifier_conf      REAL,
    classifier_reasoning TEXT,
    needs_classification BOOLEAN DEFAULT FALSE,
    error           TEXT,
    retry_count     INT DEFAULT 0,
    next_attempt_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);
-- Worker query: status='queued' OR (status='failed' AND next_attempt_at <= NOW())
-- Two partial indexes: queued items pick up ASAP, failed items respect backoff.
CREATE INDEX captures_queued ON captures(created_at) WHERE status = 'queued';
CREATE INDEX captures_failed_due ON captures(next_attempt_at)
    WHERE status = 'failed' AND next_attempt_at IS NOT NULL;
CREATE INDEX captures_created_at_desc ON captures(created_at DESC);

CREATE TABLE folder_embeddings (
    folder_id   TEXT PRIMARY KEY,
    folder_name TEXT NOT NULL,
    parent_path TEXT NOT NULL,
    embedding   VECTOR(1536),                    -- pgvector, text-embedding-3-small
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX folder_embeddings_parent ON folder_embeddings(parent_path);

CREATE TABLE topic_aliases (
    parent_path TEXT NOT NULL,                   -- e.g. Sources/Socials/Instagram
    alias       TEXT NOT NULL,                   -- "Cooking"
    canonical   TEXT NOT NULL,                   -- "Recipes"
    PRIMARY KEY (parent_path, alias)
);
```

Migrations under `ingest/migrations/` using `alembic` or plain SQL files
applied at container startup.

---

## 11. Repo layout

```
afffine-selfhost (= portainer-stack/)
├── compose.yaml                 MODIFIED — add `ingest`, remove `ollama`
├── .env.example                 MODIFIED — add INGEST_*, ANTHROPIC_API_KEY, OPENAI_API_KEY
├── prepare.sh                   unchanged
├── README.md                    MODIFIED — add ingest section
├── workspace-readme.md          existing
├── folder-organizer-prompt.md   existing
├── docs/
│   ├── specs/
│   │   └── 2026-05-06-ingest-service-design.md   (this file)
│   └── ios-app-spec.md          handoff for separate iOS repo
├── mcp-ext/                     existing
├── mcp-agent/                   staged at build (from ../affine-mcp-agent)
└── ingest/                      NEW — tracked directly here
    ├── Dockerfile
    ├── pyproject.toml
    ├── topics.yaml
    ├── migrations/
    │   └── 0001_init.sql
    ├── tests/
    └── src/
        ├── api.py               FastAPI app + routes
        ├── worker.py            asyncio task loop
        ├── db.py                asyncpg pool + queries
        ├── config.py            env + topics.yaml loader
        ├── mcp_client.py        HTTP MCP client → mcp_ext:3100
        └── pipeline/
            ├── router.py        URL → platform identity
            ├── classifier.py    Anthropic call + similarity check
            ├── filer.py         folder resolve / create / move / append
            └── extractors/
                ├── __init__.py  registry
                ├── ytdlp.py
                ├── markitdown.py
                ├── oembed_ytdlp.py
                └── reddit_json.py

../affine-mcp-agent/  (local-only, staged into mcp-agent/)
└── src/automations/
    └── sources-reorg.ts          NEW
```

---

## 12. Compose changes (excerpt)

```yaml
# REMOVE: ollama, ollama_preload services + their volume `ollama_data`

# ADD:
ingest:
  build:
    context: ./ingest
    dockerfile: Dockerfile
  image: affine-ingest:local
  pull_policy: build
  container_name: affine_ingest
  depends_on:
    mcp_ext:
      condition: service_healthy
    postgres:
      condition: service_healthy
  ports:
    - '${INGEST_BIND:-0.0.0.0}:${INGEST_PORT:-3200}:3200'
  environment:
    - PORT=3200
    - INGEST_API_TOKEN=${INGEST_API_TOKEN}
    - DATABASE_URL=postgresql://${DB_USERNAME}:${DB_PASSWORD}@postgres:5432/affine_ingest
    - MCP_EXT_URL=http://mcp_ext:3100
    - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
    - OPENAI_API_KEY=${OPENAI_API_KEY}
    - MAX_TRANSCRIPT_MIN=${MAX_TRANSCRIPT_MIN:-30}
    - REORG_THRESHOLD_DEFAULT=${REORG_THRESHOLD_DEFAULT:-15}
    - TZ=${TZ:-UTC}
  tmpfs:
    - /tmp/ingest:size=2g    # ephemeral; deleted on container restart by design
  restart: unless-stopped
  healthcheck:
    test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:3200/health').status==200 else 1)"]
    interval: 15s
    timeout: 5s
    retries: 10
    start_period: 30s
  networks:
    - affine_net
    - shared_net

# ADD: one-shot init job (mirrors the affine_migration pattern). Creates the
# `affine_ingest` database, enables pgvector, and applies SQL migrations from
# ingest/migrations/. Idempotent — safe to re-run on every stack update.
ingest_migration:
  image: affine-ingest:local
  pull_policy: build
  build:
    context: ./ingest
    dockerfile: Dockerfile
  container_name: affine_ingest_migration
  depends_on:
    postgres:
      condition: service_healthy
  command: ["python", "-m", "src.migrate"]
  environment:
    - DATABASE_URL=postgresql://${DB_USERNAME}:${DB_PASSWORD}@postgres:5432/affine_ingest
    - DB_ADMIN_URL=postgresql://${DB_USERNAME}:${DB_PASSWORD}@postgres:5432/postgres
  restart: 'no'
  networks:
    - affine_net

# Then add to ingest service:
#   depends_on:
#     ingest_migration: { condition: service_completed_successfully }
```

---

## 13. Auth & secrets

| Secret | Where | Purpose |
|---|---|---|
| `INGEST_API_TOKEN` | stack env | iOS bearer token. 32 random bytes. |
| `ANTHROPIC_API_KEY` | stack env | Classification + reorg LLM calls. |
| `OPENAI_API_KEY` | stack env | Whisper transcription + embeddings. |
| `AFFINE_ACCESS_TOKEN` | stack env | Already exists. Used by ingest via `mcp_ext`. |

Token rotation: edit stack env in Portainer → Update stack → containers
restart. iOS app prompts for new token on next call (401 handling).

---

## 14. Idempotency & retry semantics

- **Same URL twice** → returns the existing capture row, no work enqueued.
  iOS displays "already saved" with the existing doc URL.
- **`POST /retry`** → bypasses idempotency, force re-extracts. Body content
  is **replaced**, not appended (we set a marker block id and replace it).
- **Worker crash mid-extraction** → on container restart, the worker
  scans `captures WHERE status IN ('extracting','classifying','filing')` and
  re-enqueues. Each step is internally idempotent (e.g., `create_doc` is
  skipped if `doc_id` already set on the row).

---

## 15. Logging & observability

- Structured JSON to stdout (Portainer captures container logs).
- Each request has a `capture_id` correlation token logged on every line.
- One log per state transition: `extracting → classifying → filing → done`.
- Errors include the platform, URL host (not full URL — could be sensitive),
  and exception class.
- `GET /health` exposes `queue_depth`, `worker_alive` for upstream
  monitoring (cron, uptime check, etc.).

---

## 16. Testing strategy

**Unit (pytest, in-container):**
- Platform router: 30+ URLs across the routing table → expected platform.
- Classifier prompt assembly: golden-file comparison.
- Embedding similarity: synthetic pairs, expect aliases collapse.

**Integration (testcontainers):**
- Full pipeline against a fake `mcp_ext` (FastAPI mock) and recorded LLM
  responses (`vcr.py` cassettes).
- Idempotency: submit the same URL 3× concurrently → exactly one doc, two
  no-op responses.

**Manual verification (post-deploy):**
- Curl `POST /capture` with: a YouTube URL, an arxiv URL, an Instagram reel,
  an X tweet, a long article. Each yields a doc in the right place within
  60 s of stub creation.
- Reorg: seed `Sources/Socials/Instagram/Recipes` with 20 fake docs, run
  `docker exec affine_mcp_agent npx tsx src/automations/sources-reorg.ts`,
  verify 2–5 sub-folders appeared.

---

## 17. Out of scope (v1)

Repeated for emphasis — these are deliberate omissions, not oversights:

- APNs push when capture done.
- Multi-user auth, per-user folders, ACL.
- File / image upload from iOS share sheet (only URL + `shared_text`).
- Web admin UI.
- Auto re-classification when `topics.yaml` whitelist changes.
- Streaming partial results to the iOS app (no SSE/WebSocket).
- Local Ollama fallback when API down. Failure → status `failed`, manual
  retry.

---

## 18. Future work (v2+)

- APNs push integration (Apple Developer cert + service worker pattern).
- File upload endpoint accepting share-sheet attachments (PDFs, images).
- Browser extension companion (same `/capture` endpoint, no iOS needed).
- Cross-platform clients: Android share intent, macOS share extension.
- Smarter dedup: detect when the same content is shared from two platforms
  (e.g., Twitter and the article it links to) → cross-link instead of
  duplicate.
