# Phase 1 — Compose Foundation + DB Migration Job

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring up an empty `ingest` Python container with `/health` endpoint responding, the `affine_ingest` postgres database created with the full schema applied, and a migration job container that can be re-run idempotently.

**Macro plan:** [`docs/plans/2026-05-06-ingest-service-macro-plan.md`](./2026-05-06-ingest-service-macro-plan.md) — Phase 1
**Spec:** [`docs/specs/2026-05-06-ingest-service-design.md`](../specs/2026-05-06-ingest-service-design.md) — §10 (schema), §12 (compose changes), §13 (env)

**Architecture:** Single Python 3.12 container running FastAPI for the `/health` endpoint. A sibling one-shot container (`ingest_migration`) runs `python -m src.migrate` against the existing `pgvector/pgvector:pg16` postgres to create the `affine_ingest` database, enable the `vector` extension, and apply SQL migrations from `ingest/migrations/`. Both containers share one Docker image built from `ingest/Dockerfile`.

**Tech Stack:**
- Python 3.12 · FastAPI 0.115+ · uvicorn · asyncpg · pydantic-settings
- Pytest + pytest-asyncio + httpx ASGI transport for tests
- Docker / Docker Compose

**End state for Phase 1:**
- `docker compose build ingest ingest_migration` succeeds.
- `docker compose up -d` starts the stack; `affine_ingest_migration` exits 0; `affine_ingest` becomes healthy.
- `psql -d affine_ingest` shows `captures`, `folder_embeddings`, `topic_aliases` tables with all indexes from spec §10.
- `curl http://localhost:3200/health` returns `{"ok": true, "queue_depth": 0, "worker_alive": false, "version": "0.1.0"}`.
- No ollama services in compose. `.env.example` has the new ingest env block.

---

## Task 0: Pre-flight — resolve pending compose.yaml change

The branch `feat/phase-1-compose-and-db` carries an uncommitted working-tree change to `compose.yaml` that **adds** ollama services. Our design dropped ollama (all inference via API). This change must be discarded before Phase 1 edits, otherwise the diff for Phase 1's compose changes will conflate with it.

**Files:**
- Working-tree-only: `compose.yaml` (uncommitted change to be discarded)

- [ ] **Step 0.1: Inspect what would be discarded**

```bash
git diff compose.yaml | head -80
```

Expected: ~60 lines starting with `+` adding `ollama:` and `ollama_preload:` services and `ollama_data:` volume.

- [ ] **Step 0.2: Discard the unstaged ollama additions**

```bash
git restore compose.yaml
git status -sb
```

Expected: clean working tree on `feat/phase-1-compose-and-db`. No `M compose.yaml`.

> **If the user wants to keep ollama for some other reason**, stop and ask. Do not proceed with Phase 1 edits while compose.yaml has un-related uncommitted changes.

---

## Task 1: Python package scaffold

Create the bare Python project that subsequent tasks fill in.

**Files:**
- Create: `ingest/pyproject.toml`
- Create: `ingest/Dockerfile`
- Create: `ingest/.dockerignore`
- Create: `ingest/src/__init__.py`
- Create: `ingest/src/config.py`
- Create: `ingest/tests/__init__.py`

- [ ] **Step 1.1: Create `ingest/pyproject.toml`**

```toml
[project]
name = "affine-ingest"
version = "0.1.0"
description = "AFFiNE ingest service — captures URLs from iOS, files into Sources/"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "asyncpg>=0.29",
    "pydantic>=2.7",
    "pydantic-settings>=2.5",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "httpx>=0.27",
]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["."]
include = ["src*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 1.2: Create `ingest/Dockerfile`**

```dockerfile
FROM python:3.12-slim

# System deps: ffmpeg (used by yt-dlp/markitdown in later phases),
# build essentials kept minimal — no compilers, asyncpg ships wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for layer caching.
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy application code last.
COPY src/ ./src/
COPY migrations/ ./migrations/

ENV PYTHONUNBUFFERED=1
EXPOSE 3200

# Default command starts the HTTP API. The migration container overrides
# this via `command: ["python", "-m", "src.migrate"]` in compose.yaml.
CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "3200"]
```

- [ ] **Step 1.3: Create `ingest/.dockerignore`**

```
tests/
**/__pycache__/
**/*.pyc
.pytest_cache/
.mypy_cache/
.venv/
.env
*.egg-info/
```

- [ ] **Step 1.4: Create `ingest/src/__init__.py`**

(Empty file — marks `src` as a package.)

```python
```

- [ ] **Step 1.5: Create `ingest/src/config.py`**

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment.

    Phase 1 only needs PORT, DATABASE_URL, DB_ADMIN_URL, INGEST_API_TOKEN.
    Later phases extend this — never delete fields, only add.
    """

    port: int = 3200
    database_url: str = "postgresql://placeholder@localhost/affine_ingest"
    db_admin_url: str | None = None
    ingest_api_token: str = "dev-token"
    version: str = "0.1.0"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)


settings = Settings()
```

> **Why placeholder defaults?** Lets `pytest` and `python -c "from src import api"` work locally without an `.env`. Production `.env` always overrides via the compose file's `environment:` block.

- [ ] **Step 1.6: Create `ingest/tests/__init__.py`**

(Empty.)

```python
```

- [ ] **Step 1.7: Verify the package imports cleanly**

```bash
cd ingest && python -c "from src.config import settings; print(settings.port)"
```

Expected: `3200`

- [ ] **Step 1.8: Commit**

```bash
git add ingest/pyproject.toml ingest/Dockerfile ingest/.dockerignore \
        ingest/src/__init__.py ingest/src/config.py ingest/tests/__init__.py
git commit -m "$(cat <<'EOF'
feat(ingest): scaffold Python package, Dockerfile, settings loader

First skeleton of the ingest service. pyproject.toml pins FastAPI +
asyncpg + pydantic-settings. Dockerfile installs ffmpeg (for later
phases) plus pip deps. config.py loads env via pydantic-settings with
permissive defaults so tests run without an .env file.

Phase 1 / Task 1 of docs/plans/2026-05-06-phase-1-compose-and-db.md
EOF
)"
```

---

## Task 2: `/health` endpoint via TDD

**Files:**
- Create: `ingest/tests/test_health.py`
- Create: `ingest/src/api.py`

- [ ] **Step 2.1: Write the failing test**

Create `ingest/tests/test_health.py`:

```python
import pytest
from httpx import ASGITransport, AsyncClient

from src.api import app


@pytest.mark.asyncio
async def test_health_returns_ok_envelope():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["queue_depth"] == 0
    assert body["worker_alive"] is False
    assert isinstance(body["version"], str) and len(body["version"]) > 0
```

- [ ] **Step 2.2: Install dev deps locally so pytest can run**

```bash
cd ingest && pip install -e ".[dev]"
```

Expected: installs fastapi, asyncpg, pydantic, pytest, pytest-asyncio, httpx.

- [ ] **Step 2.3: Run test to verify it fails**

```bash
cd ingest && pytest tests/test_health.py -v
```

Expected: `FAILED` with `ImportError: cannot import name 'app' from 'src.api'` (no `api.py` yet).

- [ ] **Step 2.4: Write minimal implementation**

Create `ingest/src/api.py`:

```python
from fastapi import FastAPI

from src.config import settings

app = FastAPI(title="affine-ingest", version=settings.version)


@app.get("/health")
async def health() -> dict:
    """Liveness + minimal observability.

    queue_depth and worker_alive are hardcoded in Phase 1; wired up in Phase 6
    once the worker loop and DB layer exist.
    """
    return {
        "ok": True,
        "queue_depth": 0,
        "worker_alive": False,
        "version": settings.version,
    }
```

- [ ] **Step 2.5: Run test to verify it passes**

```bash
cd ingest && pytest tests/test_health.py -v
```

Expected: `1 passed`.

- [ ] **Step 2.6: Commit**

```bash
git add ingest/src/api.py ingest/tests/test_health.py
git commit -m "$(cat <<'EOF'
feat(ingest): add /health endpoint with envelope schema

Returns the shape the iOS app's Settings screen expects:
{ok, queue_depth, worker_alive, version}. queue_depth and worker_alive
are hardcoded placeholders — Phase 6 wires them to the actual worker.

Phase 1 / Task 2 of docs/plans/2026-05-06-phase-1-compose-and-db.md
EOF
)"
```

---

## Task 3: Initial migration SQL

**Files:**
- Create: `ingest/migrations/0001_init.sql`

- [ ] **Step 3.1: Create `ingest/migrations/0001_init.sql`**

```sql
-- Phase 1: initial ingest service schema.
-- All statements use IF NOT EXISTS so the runner is idempotent across
-- redeploys (the migration container runs on every stack start).

-- Provided by pgvector/pgvector:pg16 image, but enable per-database.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS captures (
    id              TEXT PRIMARY KEY,                  -- ULID
    url             TEXT,
    url_hash        TEXT UNIQUE,                       -- sha256(normalized_url)
    source_app      TEXT,
    shared_title    TEXT,
    shared_text     TEXT,
    platform        TEXT NOT NULL,
    status          TEXT NOT NULL,                     -- queued|extracting|classifying|filing|done|failed|deleted
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

-- Worker pulls 'queued' items by oldest first.
CREATE INDEX IF NOT EXISTS captures_queued
    ON captures(created_at)
    WHERE status = 'queued';

-- Failed items wait until next_attempt_at <= NOW().
CREATE INDEX IF NOT EXISTS captures_failed_due
    ON captures(next_attempt_at)
    WHERE status = 'failed' AND next_attempt_at IS NOT NULL;

-- History view in iOS app sorts by newest.
CREATE INDEX IF NOT EXISTS captures_created_at_desc
    ON captures(created_at DESC);

CREATE TABLE IF NOT EXISTS folder_embeddings (
    folder_id   TEXT PRIMARY KEY,
    folder_name TEXT NOT NULL,
    parent_path TEXT NOT NULL,
    embedding   VECTOR(1536),                          -- text-embedding-3-small dim
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS folder_embeddings_parent
    ON folder_embeddings(parent_path);

CREATE TABLE IF NOT EXISTS topic_aliases (
    parent_path TEXT NOT NULL,                         -- e.g. Sources/Socials/Instagram
    alias       TEXT NOT NULL,                         -- "Cooking"
    canonical   TEXT NOT NULL,                         -- "Recipes"
    PRIMARY KEY (parent_path, alias)
);
```

- [ ] **Step 3.2: Commit**

```bash
git add ingest/migrations/0001_init.sql
git commit -m "$(cat <<'EOF'
feat(ingest): add 0001_init.sql with captures + folder_embeddings + topic_aliases

Schema matches docs/specs/2026-05-06-ingest-service-design.md §10. All
statements use IF NOT EXISTS so the migration container can re-run on
every stack restart without conflict. pgvector extension enabled
per-database (the postgres image provides the binary).

Phase 1 / Task 3 of docs/plans/2026-05-06-phase-1-compose-and-db.md
EOF
)"
```

---

## Task 4: Migration runner script

**Files:**
- Create: `ingest/src/migrate.py`

- [ ] **Step 4.1: Create `ingest/src/migrate.py`**

```python
"""Idempotent migration runner.

Two-phase startup against the existing pgvector postgres instance:

1. Connect to the admin DB (via DB_ADMIN_URL) and CREATE the
   `affine_ingest` database if missing.
2. Connect to `affine_ingest` (via DATABASE_URL) and apply every
   .sql file under migrations/ in lexical order.

Re-running is safe: `CREATE DATABASE` is gated by an existence check;
all migration SQL uses `IF NOT EXISTS`.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg

INGEST_DB = "affine_ingest"
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


async def ensure_database(admin_url: str) -> None:
    """Create the affine_ingest database if it doesn't exist."""
    conn = await asyncpg.connect(admin_url)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", INGEST_DB
        )
        if exists:
            print(f"Database {INGEST_DB!r} already exists.")
            return
        # CREATE DATABASE cannot run inside a transaction block.
        await conn.execute(f'CREATE DATABASE "{INGEST_DB}"')
        print(f"Created database {INGEST_DB!r}.")
    finally:
        await conn.close()


async def apply_migrations(target_url: str) -> None:
    """Apply every *.sql under migrations/ in filename order."""
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        raise RuntimeError(f"No migration files found in {MIGRATIONS_DIR}")

    conn = await asyncpg.connect(target_url)
    try:
        for path in files:
            sql = path.read_text(encoding="utf-8")
            print(f"Applying {path.name} ({len(sql)} chars)")
            await conn.execute(sql)
        print(f"Applied {len(files)} migration file(s).")
    finally:
        await conn.close()


async def main() -> None:
    admin_url = os.environ.get("DB_ADMIN_URL")
    target_url = os.environ.get("DATABASE_URL")
    if not admin_url:
        raise SystemExit("DB_ADMIN_URL is required")
    if not target_url:
        raise SystemExit("DATABASE_URL is required")

    await ensure_database(admin_url)
    await apply_migrations(target_url)
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4.2: Smoke-test the module loads**

```bash
cd ingest && python -c "from src.migrate import ensure_database, apply_migrations; print('ok')"
```

Expected: `ok`

> Real DB integration is verified end-to-end in Task 9 — Phase 1 doesn't
> block on writing a unit test for `ensure_database` (that needs either
> testcontainers-postgresql or a mock-asyncpg layer; the macro plan
> covers proper DB-touching tests in Phase 3 once asyncpg pool is
> introduced).

- [ ] **Step 4.3: Commit**

```bash
git add ingest/src/migrate.py
git commit -m "$(cat <<'EOF'
feat(ingest): add idempotent migration runner

Two-phase: (1) connect to admin DB and CREATE DATABASE affine_ingest if
missing, (2) connect to affine_ingest and apply every migrations/*.sql
in lexical order. Re-running is safe — every statement uses IF NOT
EXISTS guards. Driven by env DATABASE_URL + DB_ADMIN_URL.

Phase 1 / Task 4 of docs/plans/2026-05-06-phase-1-compose-and-db.md
EOF
)"
```

---

## Task 5: Compose — add `ingest_migration` service

**Files:**
- Modify: `compose.yaml` — append a new service block before the `networks:` section.

- [ ] **Step 5.1: Verify the insertion point**

```bash
grep -n '^networks:' compose.yaml
```

Expected: a line like `262:networks:` (exact line number may differ — append the new service immediately before this line, preserving the trailing blank line above `networks:`).

- [ ] **Step 5.2: Add the `ingest_migration` service**

Insert this block immediately before the `networks:` line (matching the indentation of existing services like `mcp_ext`):

```yaml
  # One-shot job: ensures `affine_ingest` database exists, enables
  # pgvector, and applies migrations. Mirrors the affine_migration pattern.
  # Idempotent — safe to run on every stack update.
  ingest_migration:
    build:
      context: ./ingest
      dockerfile: Dockerfile
    image: affine-ingest:local
    pull_policy: build
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

```

- [ ] **Step 5.3: Validate the YAML**

```bash
docker compose config --quiet
```

Expected: no output (silent success). Any YAML or schema error prints loudly.

- [ ] **Step 5.4: Commit**

```bash
git add compose.yaml
git commit -m "$(cat <<'EOF'
feat(stack): add ingest_migration one-shot service

Mirrors the affine_migration pattern: depends_on postgres healthy,
exits after running `python -m src.migrate`. Creates the
affine_ingest database and applies migrations from
ingest/migrations/. Idempotent.

Phase 1 / Task 5 of docs/plans/2026-05-06-phase-1-compose-and-db.md
EOF
)"
```

---

## Task 6: Compose — add `ingest` service

**Files:**
- Modify: `compose.yaml` — append before `networks:` section, after `ingest_migration`.

- [ ] **Step 6.1: Add the `ingest` service**

Insert immediately after the `ingest_migration` block (before `networks:`):

```yaml
  # Python ingest service. Single container runs FastAPI + asyncio worker
  # in one process. All AFFiNE writes route through mcp_ext:3100. All
  # GPU-bound inference (LLM, transcription) hits hosted APIs.
  ingest:
    build:
      context: ./ingest
      dockerfile: Dockerfile
    image: affine-ingest:local
    pull_policy: build
    container_name: affine_ingest
    depends_on:
      ingest_migration:
        condition: service_completed_successfully
      mcp_ext:
        condition: service_healthy
      postgres:
        condition: service_healthy
    ports:
      - '${INGEST_BIND:-0.0.0.0}:${INGEST_PORT:-3200}:3200'
    environment:
      - PORT=3200
      - INGEST_API_TOKEN=${INGEST_API_TOKEN:-}
      - DATABASE_URL=postgresql://${DB_USERNAME}:${DB_PASSWORD}@postgres:5432/affine_ingest
      - MCP_EXT_URL=http://mcp_ext:3100
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
      - OPENAI_API_KEY=${OPENAI_API_KEY:-}
      - MAX_TRANSCRIPT_MIN=${MAX_TRANSCRIPT_MIN:-30}
      - REORG_THRESHOLD_DEFAULT=${REORG_THRESHOLD_DEFAULT:-15}
      - TZ=${TZ:-UTC}
    tmpfs:
      - /tmp/ingest:size=2g
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

```

- [ ] **Step 6.2: Validate the YAML**

```bash
docker compose config --quiet
```

Expected: no output.

- [ ] **Step 6.3: Confirm the dependency graph reads correctly**

```bash
docker compose config --services
```

Expected output (order may vary):
```
affine
affine_migration
ingest
ingest_migration
manticoresearch
mcp_agent
mcp_ext
postgres
redis
```

(No `ollama`, no `ollama_preload`.)

- [ ] **Step 6.4: Commit**

```bash
git add compose.yaml
git commit -m "$(cat <<'EOF'
feat(stack): add ingest Python service container

FastAPI + asyncio worker in one container, port 3200. Depends on
ingest_migration (one-shot) plus mcp_ext + postgres healthy. Tmpfs
mount at /tmp/ingest (2GB cap) for ephemeral yt-dlp/markitdown
downloads. Healthcheck pings /health every 15s.

Phase 1 / Task 6 of docs/plans/2026-05-06-phase-1-compose-and-db.md
EOF
)"
```

---

## Task 7: `.env.example` updates

**Files:**
- Modify: `.env.example`

- [ ] **Step 7.1: Inspect current `.env.example`**

```bash
cat .env.example
```

Expected: existing entries for `AFFINE_REVISION`, `PORT`, `DB_USERNAME`, etc., and possibly `OLLAMA_*` variables (which you'll remove).

- [ ] **Step 7.2: Append the ingest-service block at the bottom**

Add this section at the end of `.env.example` (preserve existing entries):

```
# === Ingest service (Python, port 3200) =====================================
# Generate a strong token: `openssl rand -hex 32`. iOS app sends this as
# `Authorization: Bearer <token>`. Rotate by editing this file in Portainer
# and re-deploying the stack.
INGEST_API_TOKEN=

# Hosted API keys. Anthropic key powers classification (Haiku 4.5) and the
# weekly reorganizer (Sonnet 4.6). OpenAI key powers Whisper transcription
# and text-embedding-3-small (folder-name similarity).
ANTHROPIC_API_KEY=
OPENAI_API_KEY=

# Host port + bind interface for the ingest API. Default 0.0.0.0 means
# anything that can reach the Docker host can hit it (typically you put a
# reverse proxy in front). Set INGEST_BIND=127.0.0.1 to restrict to the
# host only and let only an internal proxy reach it.
INGEST_PORT=3200
INGEST_BIND=0.0.0.0

# Per-capture cost guards. MAX_TRANSCRIPT_MIN caps Whisper API spend
# per video (longer videos use captions only or skip transcript with a
# note). REORG_THRESHOLD_DEFAULT is the per-leaf-folder doc count above
# which the weekly reorganizer proposes a sub-cluster split.
MAX_TRANSCRIPT_MIN=30
REORG_THRESHOLD_DEFAULT=15
```

- [ ] **Step 7.3: Remove any pre-existing `OLLAMA_*` entries**

If the file contains lines like `OLLAMA_VERSION=`, `OLLAMA_BIND=`, `OLLAMA_PORT=`, `OLLAMA_KEEP_ALIVE=`, `OLLAMA_PRELOAD_MODELS=`, delete them. Verify after:

```bash
grep -i ollama .env.example
```

Expected: no matches.

- [ ] **Step 7.4: Commit**

```bash
git add .env.example
git commit -m "$(cat <<'EOF'
chore(env): document ingest service env vars, remove ollama vars

Adds INGEST_API_TOKEN, ANTHROPIC_API_KEY, OPENAI_API_KEY, INGEST_PORT,
INGEST_BIND, MAX_TRANSCRIPT_MIN, REORG_THRESHOLD_DEFAULT. Drops the
OLLAMA_* block since the ollama services were removed and all inference
now hits hosted APIs.

Phase 1 / Task 7 of docs/plans/2026-05-06-phase-1-compose-and-db.md
EOF
)"
```

---

## Task 8: Local image build verification

**Files:** none (verification only)

- [ ] **Step 8.1: Build the ingest image**

```bash
docker compose build ingest
```

Expected: clean build ending in `Successfully tagged affine-ingest:local`. Build time first run ~60–120s (apt + pip).

- [ ] **Step 8.2: Smoke-run the API container in isolation**

```bash
docker run --rm -p 3200:3200 \
  -e DATABASE_URL=postgresql://placeholder@localhost/affine_ingest \
  affine-ingest:local &
sleep 3
curl -s http://localhost:3200/health
```

Expected:
```json
{"ok":true,"queue_depth":0,"worker_alive":false,"version":"0.1.0"}
```

Stop the container:

```bash
docker ps --filter ancestor=affine-ingest:local --format '{{.ID}}' | xargs -r docker stop
```

- [ ] **Step 8.3: No commit** (verification step only)

---

## Task 9: Full stack smoke + acceptance

**Files:** none (validation only)

- [ ] **Step 9.1: Ensure required env is set**

The stack needs at minimum `DB_USERNAME`, `DB_PASSWORD`, and (for now) `INGEST_API_TOKEN` set in the local `.env` (or in your Portainer stack env). For local-only smoke, copy from `.env.example`:

```bash
test -f .env || cp .env.example .env
# Edit .env: set DB_USERNAME, DB_PASSWORD, AFFINE_WORKSPACE_ID, AFFINE_ACCESS_TOKEN
# Set INGEST_API_TOKEN to anything for the smoke (e.g., `openssl rand -hex 32`).
```

- [ ] **Step 9.2: Bring up the stack**

```bash
docker compose up -d
```

Expected: the migration container exits 0 (its job is one-shot). All other containers stay running. Watch:

```bash
docker compose ps
```

Wait until `affine_ingest` shows `healthy`. May take 30–60s after `affine` boots.

- [ ] **Step 9.3: Verify migration ran**

```bash
docker logs affine_ingest_migration
```

Expected (lines may vary slightly):
```
Created database 'affine_ingest'.       (or "already exists" on re-run)
Applying 0001_init.sql (...)
Applied 1 migration file(s).
Migration complete.
```

- [ ] **Step 9.4: Inspect the schema**

```bash
docker exec affine_postgres psql -U "$DB_USERNAME" -d affine_ingest -c '\dt'
docker exec affine_postgres psql -U "$DB_USERNAME" -d affine_ingest -c '\di'
```

Expected:
- Three tables: `captures`, `folder_embeddings`, `topic_aliases`.
- Indexes: `captures_pkey`, `captures_url_hash_key`, `captures_queued`, `captures_failed_due`, `captures_created_at_desc`, `folder_embeddings_pkey`, `folder_embeddings_parent`, `topic_aliases_pkey`.

- [ ] **Step 9.5: Verify the pgvector extension**

```bash
docker exec affine_postgres psql -U "$DB_USERNAME" -d affine_ingest -c '\dx'
```

Expected: row for `vector` extension.

- [ ] **Step 9.6: Hit `/health` from the host**

```bash
curl -s http://localhost:${INGEST_PORT:-3200}/health | python -m json.tool
```

Expected:
```json
{
    "ok": true,
    "queue_depth": 0,
    "worker_alive": false,
    "version": "0.1.0"
}
```

- [ ] **Step 9.7: Verify ollama is gone**

```bash
docker compose ps | grep -i ollama || echo "no ollama containers (expected)"
docker volume ls | grep -i ollama || echo "no ollama volumes (expected)"
```

Expected: both lines print `no ollama ... (expected)`.

- [ ] **Step 9.8: Phase acceptance checklist** (per macro plan §Phase 1)

- [ ] `docker compose build ingest ingest_migration` succeeds
- [ ] `affine_ingest_migration` exited 0
- [ ] `affine_ingest` container is `healthy`
- [ ] `affine_ingest` database exists with all 3 tables and all 8 indexes
- [ ] `vector` extension installed in `affine_ingest`
- [ ] `/health` returns the expected JSON
- [ ] No `affine_ollama` or `affine_ollama_preload` containers
- [ ] No `ollama_data` volume

If every box ticks, Phase 1 is **done**. If any fails, troubleshoot before proceeding to Phase 2.

- [ ] **Step 9.9: Push the branch**

```bash
git push -u origin feat/phase-1-compose-and-db
```

> Sandbox restricts pushing directly to `main`. Push the branch and the
> user merges it locally (`git push origin HEAD:main` after fast-forward)
> or via the GitHub PR UI.

---

## Spec coverage map (self-review)

| Spec section | Phase 1 task |
|---|---|
| §10 captures table | Task 3 |
| §10 folder_embeddings table | Task 3 |
| §10 topic_aliases table | Task 3 |
| §10 indexes | Task 3 |
| §12 `ingest_migration` service | Task 5 |
| §12 `ingest` service | Task 6 |
| §12 ollama removed | (no-op — already absent in HEAD; Task 0 discards user's pending re-add) |
| §13 INGEST_API_TOKEN env | Task 7 |
| §13 ANTHROPIC_API_KEY / OPENAI_API_KEY env | Task 7 |
| §15 logging | (deferred to Phase 9 per macro) |
| §4 GET /health | Task 2 |
| §4 POST /capture and others | (Phase 3+ per macro) |

Phase 1 is fully covered. No placeholders. Nothing referenced in later tasks that isn't defined here.

---

## Out of scope for Phase 1 (don't sneak in)

- `POST /capture` and any other endpoint beyond `/health`.
- DB layer (`src/db.py` with asyncpg pool) — Phase 3.
- Auth middleware — Phase 3 (token check only matters when you have a real endpoint).
- Worker loop — Phase 6.
- Any extractor (`yt-dlp`, `markitdown`) — Phase 4.
- Classifier — Phase 5.
- Tests that hit real postgres — Phase 3 introduces the asyncpg pool fixture.
