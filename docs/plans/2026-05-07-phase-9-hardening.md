# Phase 9 — Hardening (logging + cost guards + smoke + README)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Production-ready posture for v1. Structured JSON logging with `capture_id` correlation across api + worker. Tests covering the cost-guard paths (`MAX_TRANSCRIPT_MIN`, `MAX_BODY_CHARS`). End-to-end smoke script for real-stack verification. README updated with bring-up + troubleshooting.

**Macro plan:** [`docs/plans/2026-05-06-ingest-service-macro-plan.md`](./2026-05-06-ingest-service-macro-plan.md) — Phase 9
**Spec:** [`docs/specs/2026-05-06-ingest-service-design.md`](../specs/2026-05-06-ingest-service-design.md) — §15 (logging), §16 (testing)

**Phase 9 is short:** ~5 commits, mostly observability + docs + verification scripts. No new features.

---

## Task 1: Structured JSON logging

Single module `src/logging_setup.py` configures Python `logging` to emit one JSON line per log record (stdout). A `contextvars`-backed `capture_id` token gets injected into every record automatically while a capture is being processed.

**Files:**
- Create: `ingest/src/logging_setup.py`
- Create: `ingest/tests/test_logging_setup.py`
- Modify: `ingest/src/api.py` — call `setup_logging()` in lifespan startup
- Modify: `ingest/src/worker.py` — use `set_capture_id` context manager around `process_fn` calls
- Modify: `ingest/src/migrate.py` — replace `print(...)` with structured logger
- Modify: `ingest/src/pipeline/orchestrator.py` — log key transitions

- [ ] **Step 1.1: Write failing tests**

`ingest/tests/test_logging_setup.py`:

```python
import io
import json
import logging

import pytest

from src.logging_setup import (
    JsonFormatter,
    capture_id_var,
    set_capture_id,
    setup_logging,
)


def _format_one(record: logging.LogRecord) -> dict:
    return json.loads(JsonFormatter().format(record))


def test_json_formatter_emits_required_fields():
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="x.py", lineno=1,
        msg="hello", args=(), exc_info=None,
    )
    payload = _format_one(record)
    assert payload["msg"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test"
    assert "ts" in payload


def test_json_formatter_includes_capture_id_when_set():
    token = capture_id_var.set("01J-X")
    try:
        record = logging.LogRecord("test", logging.INFO, "x", 1, "msg", (), None)
        payload = _format_one(record)
        assert payload["capture_id"] == "01J-X"
    finally:
        capture_id_var.reset(token)


def test_json_formatter_omits_capture_id_when_unset():
    record = logging.LogRecord("test", logging.INFO, "x", 1, "msg", (), None)
    payload = _format_one(record)
    assert "capture_id" not in payload


def test_set_capture_id_context_manager_resets():
    """The token is removed when the with block exits."""
    assert capture_id_var.get(None) is None
    with set_capture_id("01J-A"):
        assert capture_id_var.get(None) == "01J-A"
    assert capture_id_var.get(None) is None


def test_setup_logging_attaches_json_formatter_to_root(monkeypatch, caplog):
    """After setup_logging(), the root handler's formatter is JsonFormatter."""
    setup_logging(level="INFO")
    root = logging.getLogger()
    assert any(isinstance(h.formatter, JsonFormatter) for h in root.handlers)


def test_extra_fields_included_in_payload():
    record = logging.LogRecord("test", logging.INFO, "x", 1, "msg", (), None)
    record.platform = "instagram"
    record.duration_ms = 123
    payload = _format_one(record)
    assert payload["platform"] == "instagram"
    assert payload["duration_ms"] == 123
```

- [ ] **Step 1.2: Implement `ingest/src/logging_setup.py`**

```python
"""Structured JSON logging for the ingest service.

One JSON line per log record on stdout. Portainer / docker logs aggregate
these; pipe through `jq` for ad-hoc inspection.

A contextvars-backed `capture_id` token is auto-included in every record
emitted while inside a `set_capture_id(...)` block — the worker wraps
each capture's pipeline in this so all related log lines share the
correlation key.

Usage:
    setup_logging()                       # at startup
    with set_capture_id("01J-X"):         # in the worker
        await process_capture(row, ...)
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


capture_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "capture_id", default=None,
)


# Standard LogRecord attributes — anything else is treated as user-supplied
# `extra=` and emitted into the JSON payload.
_STANDARD_RECORD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "asctime", "message", "taskName",
})


class JsonFormatter(logging.Formatter):
    """Render LogRecord as a single JSON line with capture_id correlation."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        cap = capture_id_var.get(None)
        if cap:
            payload["capture_id"] = cap

        # Attach exception info if present.
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        # Pick up user-supplied extras (anything not in the standard attrs).
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRS or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)

        return json.dumps(payload, ensure_ascii=False)


def setup_logging(*, level: str | int = "INFO") -> None:
    """Configure root logger to emit JSON lines on stdout. Idempotent."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.setLevel(level)
    # Drop any existing handlers (e.g., uvicorn's default) so we don't
    # double-log.
    root.handlers[:] = [handler]
    root.propagate = False


@contextlib.contextmanager
def set_capture_id(capture_id: str):
    """Bind capture_id to the current async/sync context."""
    token = capture_id_var.set(capture_id)
    try:
        yield
    finally:
        capture_id_var.reset(token)
```

- [ ] **Step 1.3: Wire into `api.py` and `worker.py`**

In `api.py` lifespan (top of the function, before pool init):

```python
from src.logging_setup import setup_logging
setup_logging(level=os.environ.get("LOG_LEVEL", "INFO"))
```

In `worker.py`'s `_loop`, wrap each row's processing block:

```python
from src.logging_setup import set_capture_id

# ... existing code ...
async def _loop(self) -> None:
    self._alive = True
    try:
        while not self._stop.is_set():
            row = await self._claim_next()
            if row is None:
                # idle as before
                ...
                continue

            with set_capture_id(row.id):
                try:
                    # existing process call
                    ...
                except Exception as exc:
                    await self._handle_failure(row, exc)
    finally:
        self._alive = False
```

In `migrate.py`, replace `print(...)` with `logging.getLogger("migrate").info(...)`. Setup_logging isn't called in migrate (it runs as a one-shot before the API starts), so add a minimal init at the top:

```python
import logging

if __name__ == "__main__":
    from src.logging_setup import setup_logging
    setup_logging()
    asyncio.run(main())
```

And replace each `print(...)` inside ensure_database/apply_migrations with `logging.getLogger(__name__).info(...)`.

In `orchestrator.py`, add log lines at each transition:

```python
import logging
log = logging.getLogger(__name__)

# In process_capture, after each step:
log.info("transition", extra={"step": "extracted", "platform": platform.id})
log.info("transition", extra={"step": "classified", "topic": result.topic, "confidence": result.confidence})
log.info("transition", extra={"step": "filed", "topic_path": topic_path})
log.info("transition", extra={"step": "done"})
```

- [ ] **Step 1.4: Run tests**

```bash
cd ingest && python -m pytest tests/test_logging_setup.py -v
```

Expected: 6 passed.

Run full suite to ensure nothing broke:
```bash
python -m pytest tests/
```

Expected: ~180 passed, 5 skipped.

- [ ] **Step 1.5: Commit**

```bash
git add ingest/src/logging_setup.py ingest/src/api.py ingest/src/worker.py \
        ingest/src/migrate.py ingest/src/pipeline/orchestrator.py \
        ingest/tests/test_logging_setup.py
git commit -m "$(cat <<'EOF'
feat(ingest): structured JSON logging with capture_id correlation

JsonFormatter emits one line per record with ts/level/logger/msg + any
user-supplied `extra=` kwargs. A contextvars-backed capture_id token
auto-injects into every record while inside a set_capture_id() block —
the worker wraps each capture's pipeline so all related lines share
the correlation key.

setup_logging() called from FastAPI lifespan and migrate.py main.
print() in migrate.py replaced with structured log calls. Orchestrator
gains one transition log per state change with platform/topic/confidence
in the extra payload.

Phase 9 / Task 1 of docs/plans/2026-05-07-phase-9-hardening.md
EOF
)"
```

---

## Task 2: Cost guard tests

The spec calls out `MAX_TRANSCRIPT_MIN` and `MAX_BODY_CHARS` as cost-guards. Both are tested implicitly in Phase 4 (yt-dlp extractor skips long videos; truncate_body caps at limit). This task adds *explicit* end-to-end style tests that pin the contract.

**Files:**
- Create: `ingest/tests/test_cost_guards.py`

- [ ] **Step 2.1: Write tests**

```python
"""End-to-end pin tests for the cost-guard paths.

These overlap with the Phase 4 extractor unit tests but cover the contract
explicitly and survive future refactors of the extractor internals.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import Platform, settings
from src.pipeline.extracted import truncate_body


def test_truncate_body_caps_at_max_body_chars():
    body = "x" * (settings.max_body_chars * 2)
    out = truncate_body(body, limit=settings.max_body_chars)
    assert len(out) <= settings.max_body_chars + 80  # marker is short
    assert out.endswith("[...truncated]")


def test_max_body_chars_default_is_50_000():
    assert settings.max_body_chars == 50_000


def test_max_transcript_min_default_is_30():
    assert settings.max_transcript_min == 30


@pytest.mark.asyncio
async def test_ytdlp_extractor_honors_max_transcript_min():
    """A 90-min YouTube without captions must NOT call Whisper API."""
    from src.pipeline.extractors.ytdlp_ext import extract

    info = {
        "id": "long-id",
        "title": "Long Video",
        "channel": "Ch",
        "duration": 90 * 60,  # 90 minutes — over the 30min cap
        "upload_date": "20260507",
        "description": "long video",
        "subtitles": {},
        "automatic_captions": {},
    }

    plat = Platform(id="youtube", group="Socials", folder_name="Youtube",
                    hosts=["youtube.com"], extractor="ytdlp")

    with patch("src.pipeline.extractors.ytdlp_ext._run_ytdlp_metadata", new_callable=AsyncMock) as run, \
         patch("src.pipeline.extractors.ytdlp_ext._run_ytdlp_audio", new_callable=AsyncMock) as audio, \
         patch("src.pipeline.extractors.ytdlp_ext._whisper_transcribe", new_callable=AsyncMock) as whisper:
        # Fake workdir with just the info.json (no caption files)
        import tempfile
        workdir = Path(tempfile.mkdtemp())
        (workdir / "video.info.json").write_text(json.dumps(info), encoding="utf-8")
        run.return_value = workdir

        result = await extract("https://www.youtube.com/watch?v=long", plat)

        # Cost guard: no audio extraction, no whisper call.
        audio.assert_not_called()
        whisper.assert_not_called()
        assert "transcript skipped" in result.body_md.lower()


@pytest.mark.asyncio
async def test_ytdlp_extractor_uses_caption_when_present_no_whisper():
    """Even for a short video, presence of caption skips Whisper (cost saver)."""
    from src.pipeline.extractors.ytdlp_ext import extract

    info = {
        "id": "short-id",
        "title": "Short Video",
        "channel": "Ch",
        "duration": 600,  # 10 min, under cap
        "upload_date": "20260507",
        "description": "short video",
    }

    plat = Platform(id="youtube", group="Socials", folder_name="Youtube",
                    hosts=["youtube.com"], extractor="ytdlp")

    with patch("src.pipeline.extractors.ytdlp_ext._run_ytdlp_metadata", new_callable=AsyncMock) as run, \
         patch("src.pipeline.extractors.ytdlp_ext._run_ytdlp_audio", new_callable=AsyncMock) as audio, \
         patch("src.pipeline.extractors.ytdlp_ext._whisper_transcribe", new_callable=AsyncMock) as whisper:
        import tempfile
        workdir = Path(tempfile.mkdtemp())
        (workdir / "video.info.json").write_text(json.dumps(info), encoding="utf-8")
        (workdir / "video.en.vtt").write_text(
            "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello\n", encoding="utf-8",
        )
        run.return_value = workdir

        result = await extract("https://www.youtube.com/watch?v=short", plat)

        audio.assert_not_called()
        whisper.assert_not_called()
        assert "hello" in result.body_md
```

- [ ] **Step 2.2: Run + commit**

```bash
cd ingest && python -m pytest tests/test_cost_guards.py -v
git add ingest/tests/test_cost_guards.py
git commit -m "$(cat <<'EOF'
test(ingest): pin tests for cost-guard contract

Explicit tests asserting:
  - truncate_body honors MAX_BODY_CHARS (50_000 default)
  - MAX_TRANSCRIPT_MIN defaults to 30
  - yt-dlp extractor SKIPS Whisper for video > 30min without captions
  - yt-dlp extractor SKIPS Whisper when caption is available, even
    for short video (extra cost saver)

These overlap with Phase 4 extractor tests but pin the cost-guard
contract independently — survive future refactors of extractor
internals.

Phase 9 / Task 2 of docs/plans/2026-05-07-phase-9-hardening.md
EOF
)"
```

---

## Task 3: End-to-end smoke script

A bash script that hits a running stack with three URL types and asserts the captures progress to `done` within 60s each.

**Files:**
- Create: `ingest/scripts/smoke.sh`

- [ ] **Step 3.1: Implement `ingest/scripts/smoke.sh`**

```bash
#!/usr/bin/env bash
# End-to-end smoke against a running ingest service.
#
# Usage:
#   INGEST_BASE=http://localhost:3200 \
#   INGEST_API_TOKEN=... \
#   bash ingest/scripts/smoke.sh
#
# Submits 3 representative URLs (arxiv, reddit, YouTube w/ captions),
# polls /captures/{id} until each reaches status='done' or 60s elapses.

set -euo pipefail

: "${INGEST_BASE:=http://localhost:3200}"
: "${INGEST_API_TOKEN:?INGEST_API_TOKEN must be set}"
: "${SMOKE_TIMEOUT_SEC:=60}"

URLS=(
  "https://arxiv.org/abs/2401.00001"
  "https://www.reddit.com/r/python/"
  "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
)

pass=0
fail=0

for url in "${URLS[@]}"; do
  echo
  echo "=== Submitting: $url ==="

  body=$(jq -n --arg url "$url" '{url: $url, source_app: "smoke"}')
  resp=$(curl -sS -m 10 -X POST "$INGEST_BASE/capture" \
              -H "Authorization: Bearer $INGEST_API_TOKEN" \
              -H "Content-Type: application/json" \
              --data "$body")

  capture_id=$(echo "$resp" | jq -r '.capture_id // empty')
  if [[ -z "$capture_id" ]]; then
    echo "FAIL: no capture_id in response: $resp"
    fail=$((fail + 1))
    continue
  fi

  echo "  capture_id=$capture_id"
  echo "  initial_path=$(echo "$resp" | jq -r '.initial_path')"
  echo "  doc_id=$(echo "$resp" | jq -r '.doc_id')"

  # Poll until done or timeout.
  start=$(date +%s)
  status="queued"
  while :; do
    elapsed=$(($(date +%s) - start))
    if (( elapsed > SMOKE_TIMEOUT_SEC )); then
      echo "  FAIL: timeout after ${SMOKE_TIMEOUT_SEC}s (last status: $status)"
      fail=$((fail + 1))
      break
    fi
    sleep 2

    detail=$(curl -sS -m 10 \
                  -H "Authorization: Bearer $INGEST_API_TOKEN" \
                  "$INGEST_BASE/captures/$capture_id")
    status=$(echo "$detail" | jq -r '.status')
    echo "  t=${elapsed}s status=$status"

    if [[ "$status" == "done" ]]; then
      topic=$(echo "$detail" | jq -r '.topic_path')
      echo "  PASS: filed under '$topic'"
      pass=$((pass + 1))
      break
    fi
    if [[ "$status" == "failed" ]]; then
      err=$(echo "$detail" | jq -r '.error // empty')
      echo "  FAIL: pipeline failed: $err"
      fail=$((fail + 1))
      break
    fi
  done
done

echo
echo "=== Smoke result: $pass passed, $fail failed ==="
test "$fail" -eq 0
```

- [ ] **Step 3.2: Make it executable + commit**

```bash
chmod +x ingest/scripts/smoke.sh
git add ingest/scripts/smoke.sh
git commit -m "$(cat <<'EOF'
test(ingest): end-to-end smoke script (curl + jq)

Submits 3 URL types (arxiv, reddit, YouTube w/ captions) to
POST /capture, polls /captures/{id} until status=done or
SMOKE_TIMEOUT_SEC (default 60) elapses. Reports topic_path on success
or error message on failure.

Run after deploy / config change:
  INGEST_BASE=http://localhost:3200 \
  INGEST_API_TOKEN=... \
  bash ingest/scripts/smoke.sh

Phase 9 / Task 3 of docs/plans/2026-05-07-phase-9-hardening.md
EOF
)"
```

---

## Task 4: README hardening section

Add a section to `portainer-stack/README.md` documenting the ingest service: env vars, bring-up, smoke instructions, troubleshooting decision tree.

**Files:**
- Modify: `portainer-stack/README.md`

- [ ] **Step 4.1: Read current README**

Identify a good insertion point — after the existing "MCP Extension Proxy" section, add a new "Ingest Service" section.

- [ ] **Step 4.2: Add the section**

```markdown
## Ingest Service

Python/FastAPI sidecar that captures URLs from the iOS share extension
and files them into AFFiNE under `Sources/`. Runs alongside `mcp_ext` +
`mcp_agent` in the same stack. Ports `${INGEST_PORT:-3200}` on the host.

### What it does

1. iOS share sheet POSTs a URL to `/capture`.
2. Service creates a stub doc in `Sources/<group>/<platform>/` (always within 500ms) and returns 202 with the doc URL — iOS shows it immediately.
3. Background asyncio worker picks the row up:
   - Extracts content (yt-dlp / markitdown / oEmbed / reddit JSON depending on URL host).
   - Classifies into a topic via Claude Haiku 4.5 with prompt caching.
   - Uses cosine-similarity on folder-name embeddings (OpenAI text-embedding-3-small) to dedup near-duplicate folders ("Cooking" → existing "Recipes").
   - Moves the doc into the topic folder + appends the extracted body via `mcp_ext`.
4. Weekly Sunday 03:00 UTC cron in `mcp_agent` (`sources-reorg`) sweeps `Sources/*` for over-15-doc leaf folders and proposes 2–5 sub-clusters via Claude Sonnet 4.6.

### Required environment variables

| Variable | Purpose |
|---|---|
| `INGEST_API_TOKEN` | Bearer token iOS sends. Generate: `openssl rand -hex 32`. |
| `ANTHROPIC_API_KEY` | Classifier (Haiku 4.5) + reorganizer (Sonnet 4.6). |
| `OPENAI_API_KEY` | Whisper API (audio transcription) + embeddings. |
| `INGEST_PORT` | Host port (default 3200). |
| `INGEST_BIND` | Bind interface (default 0.0.0.0; set 127.0.0.1 to restrict). |
| `MAX_TRANSCRIPT_MIN` | Whisper cost guard (default 30 min/video). |
| `REORG_THRESHOLD_DEFAULT` | Reorganizer threshold per leaf folder (default 15 docs). |

### Bring-up

```bash
# 1. Make sure your .env has the keys above (copy from .env.example).
# 2. Build + deploy the stack via Portainer (Stacks → Update).
# 3. Verify the service comes up healthy:
docker compose ps                       # affine_ingest = healthy
curl http://localhost:3200/health       # {"ok": true, ...}

# 4. End-to-end smoke (sends 3 URLs, checks they land in AFFiNE):
INGEST_BASE=http://localhost:3200 \
INGEST_API_TOKEN=$INGEST_API_TOKEN \
bash ingest/scripts/smoke.sh
```

### Inspecting captures

```bash
# Recent captures (any status)
curl -H "Authorization: Bearer $INGEST_API_TOKEN" \
  http://localhost:3200/captures?limit=20 | jq

# Single capture detail
curl -H "Authorization: Bearer $INGEST_API_TOKEN" \
  http://localhost:3200/captures/<id> | jq

# Retry a failed capture
curl -X POST -H "Authorization: Bearer $INGEST_API_TOKEN" \
  http://localhost:3200/captures/<id>/retry

# In Postgres directly
docker exec -it affine_postgres psql -U affine -d affine_ingest \
  -c "SELECT id, platform, status, topic_path, retry_count FROM captures ORDER BY created_at DESC LIMIT 20;"
```

### Logs

Structured JSON, one line per record. Pipe through jq:

```bash
docker logs affine_ingest --since 10m -f | jq -c '. | {ts, level, msg, capture_id, step, topic}'
```

Lines emitted while a capture is being processed include `capture_id` so you can grep a single flow:

```bash
docker logs affine_ingest --since 1h | jq -c 'select(.capture_id == "01J-X")'
```

### Troubleshooting

**Capture stuck at `queued`**
- Worker not running. Check `/health` — `worker_alive` should be `true`. If false, restart `affine_ingest`.
- `mcp_ext` unhealthy. Check `docker compose ps mcp_ext`.

**Capture stuck at `failed`**
- Look at `error` field in the capture detail. Common causes:
  - `OPENAI_API_KEY` missing → Whisper or embedding call failed. Fix env, re-deploy.
  - `ANTHROPIC_API_KEY` missing → classifier failed.
  - URL behind login → extractor returned empty body. Manual workaround: capture as `shared_text` instead of URL, or skip.
- After 3 retries (60s, 5min, 30min backoff) the row stays at `failed` until you `POST /captures/{id}/retry` manually.

**Doc landed in `Sources/<platform>/` root instead of a topic folder**
- Classifier returned `confidence < 0.6`. Check `classifier_reasoning` in the detail. The weekly reorganizer (Sunday 03:00 UTC) revisits these.

**Reorganizer didn't run**
- Check `mcp_agent` logs for the `Sources Reorg` cron entry on Sunday. Manual trigger:
  ```bash
  docker exec -it affine_mcp_agent npx tsx src/automations/sources-reorg.ts
  ```

**Same URL captured twice**
- Should not happen — `url_hash` is UNIQUE. The second POST returns the existing capture. If you see duplicates, check that URL normalization (`utm_*` strip, lowercase host) is working: `python -c "from src.models import normalized_url; print(normalized_url('https://...'))"` inside the container.

**Cost spike on Whisper**
- Set `MAX_TRANSCRIPT_MIN` lower (default 30). Long videos auto-skip transcription.
- Or rotate `OPENAI_API_KEY` to one with budget alerts.
```

- [ ] **Step 4.3: Commit**

```bash
git add portainer-stack/README.md
git commit -m "$(cat <<'EOF'
docs(stack): add Ingest Service section to README

Covers the full v1 surface: required env vars, bring-up checklist,
smoke command, log inspection, troubleshooting decision tree for the
common failure modes (stuck queued/failed, classifier confidence floor,
reorganizer cron, dedup, Whisper cost spike).

Phase 9 / Task 4 of docs/plans/2026-05-07-phase-9-hardening.md
EOF
)"
```

---

## Task 5: Build + push + PR

```bash
cd portainer-stack && docker compose build ingest
cd ingest && python -m pytest tests/        # ~180 passed, 5 skipped
cd .. && git push -u origin feat/phase-9-hardening
gh pr create --base main --title "Phase 9: Hardening (logging + smoke + README)" --body "..."
```

---

## Spec coverage

| Phase 9 deliverable | Task |
|---|---|
| Structured JSON logging | 1 |
| `capture_id` correlation | 1 |
| Cost guard tests | 2 |
| End-to-end smoke script | 3 |
| README ingest section + troubleshooting | 4 |

## Out of scope

- Prometheus metrics / OpenTelemetry traces — JSON log lines are enough for personal scale; add later if you want to feed Grafana.
- Log rotation / external sink — Portainer/docker handles rotation; if logs grow, run `docker logs --tail` more aggressively or set `--log-opt max-size`.
- Cursor pagination implementation in `/captures` (Phase 7 left `next_cursor` as null) — defer; the iOS app refreshes by fetching the latest 50 each time.
- Re-classification pass on changed `topics.yaml.topic_hints` — no v1 use case; manual `POST /retry` per capture works.
