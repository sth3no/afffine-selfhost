# Phase 6 — Worker Loop + State Machine + Retry + Crash Recovery

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** A POST `/capture` ultimately produces a fully filed AFFiNE doc with extracted body, correct topic folder, and `status=done` in the DB — without anyone calling the worker explicitly. The worker pumps `queued` rows through `extracting → classifying → filing → done`, retries failures with exponential backoff, and resumes in-flight items after a container restart.

**Macro plan:** [`docs/plans/2026-05-06-ingest-service-macro-plan.md`](./2026-05-06-ingest-service-macro-plan.md) — Phase 6
**Spec:** [`docs/specs/2026-05-06-ingest-service-design.md`](../specs/2026-05-06-ingest-service-design.md) — §5 (state machine), §14 (idempotency + retry)
**Phase 5 prereq:** PR #11 (or its branch) provides classifier + Filer.move_to_topic_folder + DB repos.

**Architecture:**
- Single asyncio task in the same FastAPI process polls `captures` for ready work (every 2s tick).
- Per row: orchestrator runs the 4-step state machine, transitioning `status` after each step (atomic UPDATE).
- Per-step idempotency: skips work whose result is already persisted (e.g., `classifier_topic` set → don't re-classify).
- Failure: row marked `status='failed'`, `retry_count++`, `next_attempt_at = NOW + backoff`. Worker query `claim_due_failed()` picks them up at the right time.
- Crash recovery on lifespan startup: `UPDATE captures SET status='queued' WHERE status IN ('extracting','classifying','filing')` — those rows go back to the queue; per-step idempotency means they resume cleanly.
- `/health` reports live `queue_depth` (count of queued + due-failed) and `worker_alive` flag.

**Tech Stack:**
- `asyncio.create_task` for the worker loop in lifespan
- Postgres `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1` for atomic claim
- Exponential backoff schedule: `[60s, 300s, 1800s]` then permanent fail

**End-of-phase test count:** ~127 (current) + ~22 new ≈ ~149 passed, 5 skipped.

---

## Task 1: DB queries for worker lifecycle

Extend `CaptureRepository` with the queries the worker needs. All atomic UPDATEs.

**Files:**
- Modify: `ingest/src/db.py` — add methods to `CaptureRepository`
- Create: `ingest/tests/test_db_worker_queries.py`

**New methods (per spec):**

| Method | SQL shape | Purpose |
|---|---|---|
| `claim_next_queued()` | `UPDATE captures SET status='extracting', updated_at=NOW() WHERE id=(SELECT id FROM captures WHERE status='queued' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING *` | atomically claim a queued row |
| `claim_due_failed()` | same pattern but `WHERE status='failed' AND next_attempt_at <= NOW()` | claim a failed row whose retry window opened |
| `mark_classifying(id, classifier_*)` | `UPDATE ... SET status='classifying', classifier_topic=$2, classifier_conf=$3, classifier_reasoning=$4, updated_at=NOW() WHERE id=$1` | persist classifier output and advance |
| `mark_filing(id, topic_path)` | `UPDATE ... SET status='filing', topic_path=$2, updated_at=NOW() WHERE id=$1` | advance and persist topic_path |
| `mark_done(id)` | `UPDATE ... SET status='done', completed_at=NOW(), updated_at=NOW() WHERE id=$1` | terminal success |
| `mark_failed(id, error, retry_count, next_attempt_at)` | `UPDATE ... SET status='failed', error=$2, retry_count=$3, next_attempt_at=$4, updated_at=NOW() WHERE id=$1` | record failure, schedule retry |
| `count_active() -> int` | `SELECT count(*) FROM captures WHERE status IN ('queued','extracting','classifying','filing','failed')` | for `/health` queue_depth |
| `reset_in_flight_to_queued() -> int` | `UPDATE captures SET status='queued', updated_at=NOW() WHERE status IN ('extracting','classifying','filing') RETURNING id` (return count) | crash recovery on startup |

- [ ] **Step 1.1: Write the failing test**

`ingest/tests/test_db_worker_queries.py`:

```python
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from src.db import CaptureRepository


def _row_dict(**overrides):
    base = {
        "id": "01J", "url": "https://x", "url_hash": "h", "source_app": None,
        "shared_title": None, "shared_text": None, "platform": "article",
        "status": "queued", "doc_id": "d", "web_url": "w",
        "topic_path": "Sources/Articles/Web",
        "created_at": datetime(2026, 5, 7, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_claim_next_queued_returns_row_and_updates_status():
    conn = AsyncMock()
    conn.fetchrow.return_value = _row_dict(status="extracting")
    repo = CaptureRepository(conn)
    row = await repo.claim_next_queued()
    assert row is not None
    assert row.status == "extracting"
    sql = conn.fetchrow.call_args.args[0]
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "status='extracting'" in sql or "status = 'extracting'" in sql.replace(" ", "")
    assert "WHERE status='queued'" in sql or "WHERE status = 'queued'" in sql.replace(" ", "")


@pytest.mark.asyncio
async def test_claim_next_queued_returns_none_when_no_rows():
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    repo = CaptureRepository(conn)
    assert await repo.claim_next_queued() is None


@pytest.mark.asyncio
async def test_claim_due_failed_filters_on_next_attempt_at():
    conn = AsyncMock()
    conn.fetchrow.return_value = _row_dict(status="extracting")
    repo = CaptureRepository(conn)
    await repo.claim_due_failed()
    sql = conn.fetchrow.call_args.args[0]
    assert "next_attempt_at" in sql
    assert "<= NOW()" in sql.replace(" ", "<=NOW()") or "<= now()" in sql.lower()


@pytest.mark.asyncio
async def test_mark_classifying_binds_all_fields():
    conn = AsyncMock()
    repo = CaptureRepository(conn)
    await repo.mark_classifying(
        capture_id="01J",
        topic="Recipes",
        confidence=0.92,
        reasoning="dish photo",
    )
    sql, *args = conn.execute.call_args.args
    assert "status='classifying'" in sql.replace(" ", "") or "status = 'classifying'" in sql
    assert args == ["01J", "Recipes", 0.92, "dish photo"]


@pytest.mark.asyncio
async def test_mark_filing_persists_topic_path():
    conn = AsyncMock()
    repo = CaptureRepository(conn)
    await repo.mark_filing(capture_id="01J", topic_path="Sources/Socials/Instagram/Recipes")
    sql, *args = conn.execute.call_args.args
    assert "topic_path" in sql
    assert args == ["01J", "Sources/Socials/Instagram/Recipes"]


@pytest.mark.asyncio
async def test_mark_done_sets_terminal_status():
    conn = AsyncMock()
    repo = CaptureRepository(conn)
    await repo.mark_done("01J")
    sql, *args = conn.execute.call_args.args
    assert "status='done'" in sql.replace(" ", "") or "status = 'done'" in sql
    assert "completed_at" in sql
    assert args == ["01J"]


@pytest.mark.asyncio
async def test_mark_failed_schedules_retry():
    conn = AsyncMock()
    repo = CaptureRepository(conn)
    next_at = datetime(2026, 5, 7, 12, tzinfo=timezone.utc)
    await repo.mark_failed(
        capture_id="01J",
        error="boom",
        retry_count=1,
        next_attempt_at=next_at,
    )
    sql, *args = conn.execute.call_args.args
    assert "status='failed'" in sql.replace(" ", "") or "status = 'failed'" in sql
    assert args == ["01J", "boom", 1, next_at]


@pytest.mark.asyncio
async def test_count_active_returns_int():
    conn = AsyncMock()
    conn.fetchval.return_value = 7
    repo = CaptureRepository(conn)
    assert await repo.count_active() == 7
    sql = conn.fetchval.call_args.args[0]
    assert "count(*)" in sql.lower()
    assert "queued" in sql
    assert "failed" in sql


@pytest.mark.asyncio
async def test_reset_in_flight_to_queued_returns_count():
    conn = AsyncMock()
    conn.fetch.return_value = [{"id": "01J-a"}, {"id": "01J-b"}]  # two rows reset
    repo = CaptureRepository(conn)
    n = await repo.reset_in_flight_to_queued()
    assert n == 2
    sql = conn.fetch.call_args.args[0]
    assert "extracting" in sql
    assert "classifying" in sql
    assert "filing" in sql
    assert "queued" in sql
```

- [ ] **Step 1.2: Implement — append to `ingest/src/db.py`**

Add these methods to `CaptureRepository` (don't break existing `insert`/`get_by_url_hash`/`get_by_id`):

```python
    # ── Worker lifecycle queries (Phase 6) ───────────────────────────

    async def claim_next_queued(self) -> CaptureRow | None:
        """Atomically claim the oldest queued row, transitioning to 'extracting'."""
        sql = """
            UPDATE captures SET status = 'extracting', updated_at = NOW()
            WHERE id = (
                SELECT id FROM captures
                WHERE status = 'queued'
                ORDER BY created_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, url, url_hash, source_app, shared_title, shared_text,
                      platform, status, doc_id, web_url, topic_path, created_at
        """
        rec = await self._conn.fetchrow(sql)
        return None if rec is None else CaptureRow(**dict(rec))

    async def claim_due_failed(self) -> CaptureRow | None:
        """Atomically claim the oldest failed row whose retry window has opened."""
        sql = """
            UPDATE captures SET status = 'extracting', updated_at = NOW()
            WHERE id = (
                SELECT id FROM captures
                WHERE status = 'failed' AND next_attempt_at <= NOW()
                ORDER BY next_attempt_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, url, url_hash, source_app, shared_title, shared_text,
                      platform, status, doc_id, web_url, topic_path, created_at
        """
        rec = await self._conn.fetchrow(sql)
        return None if rec is None else CaptureRow(**dict(rec))

    async def mark_classifying(
        self, *, capture_id: str, topic: str | None, confidence: float, reasoning: str
    ) -> None:
        await self._conn.execute(
            """
            UPDATE captures
            SET status = 'classifying',
                classifier_topic = $2,
                classifier_conf = $3,
                classifier_reasoning = $4,
                updated_at = NOW()
            WHERE id = $1
            """,
            capture_id, topic, confidence, reasoning,
        )

    async def mark_filing(self, *, capture_id: str, topic_path: str) -> None:
        await self._conn.execute(
            """
            UPDATE captures
            SET status = 'filing', topic_path = $2, updated_at = NOW()
            WHERE id = $1
            """,
            capture_id, topic_path,
        )

    async def mark_done(self, capture_id: str) -> None:
        await self._conn.execute(
            """
            UPDATE captures
            SET status = 'done', completed_at = NOW(), updated_at = NOW()
            WHERE id = $1
            """,
            capture_id,
        )

    async def mark_failed(
        self,
        *,
        capture_id: str,
        error: str,
        retry_count: int,
        next_attempt_at: datetime | None,
    ) -> None:
        await self._conn.execute(
            """
            UPDATE captures
            SET status = 'failed',
                error = $2,
                retry_count = $3,
                next_attempt_at = $4,
                updated_at = NOW()
            WHERE id = $1
            """,
            capture_id, error, retry_count, next_attempt_at,
        )

    async def count_active(self) -> int:
        return int(await self._conn.fetchval(
            """
            SELECT count(*) FROM captures
            WHERE status IN ('queued','extracting','classifying','filing','failed')
            """
        ))

    async def reset_in_flight_to_queued(self) -> int:
        """Crash recovery: rows mid-pipeline at startup go back to 'queued'."""
        rows = await self._conn.fetch(
            """
            UPDATE captures
            SET status = 'queued', updated_at = NOW()
            WHERE status IN ('extracting','classifying','filing')
            RETURNING id
            """
        )
        return len(rows)
```

- [ ] **Step 1.3: Run + commit**

```bash
cd ingest && python -m pytest tests/test_db_worker_queries.py -v
git add ingest/src/db.py ingest/tests/test_db_worker_queries.py
git commit -m "$(cat <<'EOF'
feat(ingest): worker DB queries (claim + transitions + recovery)

Extends CaptureRepository with the 8 queries the worker needs:
  - claim_next_queued / claim_due_failed: SELECT ... FOR UPDATE SKIP
    LOCKED LIMIT 1 + atomic transition to 'extracting'.
  - mark_classifying / mark_filing / mark_done: status transitions that
    also persist the artifact produced by that step.
  - mark_failed: record error + retry_count + next_attempt_at for the
    backoff scheduler to pick up.
  - count_active: feeds /health queue_depth.
  - reset_in_flight_to_queued: lifespan crash-recovery that flips any
    extracting/classifying/filing row back to queued so the worker
    resumes it.

Phase 6 / Task 1 of docs/plans/2026-05-07-phase-6-worker.md
EOF
)"
```

---

## Task 2: Pipeline orchestrator

Single function `process_capture(row, deps) -> None` that runs the 4-step pipeline for one row. Each step transitions status; per-step idempotency skips already-completed work on retry.

**Files:**
- Create: `ingest/src/pipeline/orchestrator.py`
- Create: `ingest/tests/test_orchestrator.py`

**State diagram:**
```
queued → extracting (claim) → run extractor → mark_classifying(topic, conf, reason)
classifying → run move_to_topic_folder → mark_filing(topic_path)
filing → run move_document + append body blocks → mark_done
```

- [ ] **Step 2.1: Write failing test**

`ingest/tests/test_orchestrator.py`:

```python
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import Platform, TopicsConfig
from src.db import CaptureRow
from src.pipeline.classification import ClassificationResult
from src.pipeline.extracted import Extracted, MediaKind
from src.pipeline.orchestrator import process_capture


def _row(status="extracting", **kw):
    base = {
        "id": "01J", "url": "https://example.com/article",
        "url_hash": "h", "source_app": None, "shared_title": None, "shared_text": None,
        "platform": "article", "status": status, "doc_id": "d-1", "web_url": "w",
        "topic_path": "Sources/Articles/Web",
    }
    base.update(kw)
    from datetime import datetime, timezone
    base["created_at"] = datetime(2026, 5, 7, tzinfo=timezone.utc)
    return CaptureRow(**base)


def _platform() -> Platform:
    return Platform(id="article", group="Articles", folder_name="Web",
                    hosts=["*"], extractor="markitdown")


def _extracted() -> Extracted:
    return Extracted(
        title="Hello",
        body_md="# Body\n\nContent here.",
        author="someone",
        published_at=None,
        media_kind=MediaKind.TEXT,
        extra={},
    )


def _topics(plat: Platform) -> TopicsConfig:
    return TopicsConfig(platforms=[plat], topic_hints={"article": ["Tech", "Science"]})


@pytest.fixture
def deps():
    """Shared mock dependency bundle."""
    repo = AsyncMock()
    filer = AsyncMock()
    filer._mcp = AsyncMock()
    extract_fn = AsyncMock(return_value=_extracted())
    classify_fn = AsyncMock(return_value=ClassificationResult(
        topic="Tech", confidence=0.92, reasoning="article about tech",
    ))
    return {
        "repo": repo,
        "filer": filer,
        "extract_fn": extract_fn,
        "classify_fn": classify_fn,
    }


@pytest.mark.asyncio
async def test_process_capture_happy_path_advances_through_all_states(deps):
    plat = _platform()
    deps["filer"].move_to_topic_folder.return_value = "f-tech"
    deps["filer"]._mcp.list_folder_tree.return_value = {"totalNodes": 0, "tree": []}

    await process_capture(
        _row(),
        platform=plat,
        topics=_topics(plat),
        repo=deps["repo"],
        filer=deps["filer"],
        extract_fn=deps["extract_fn"],
        classify_fn=deps["classify_fn"],
    )

    deps["extract_fn"].assert_awaited_once()
    deps["classify_fn"].assert_awaited_once()
    deps["repo"].mark_classifying.assert_awaited_once()
    deps["repo"].mark_filing.assert_awaited_once()
    deps["repo"].mark_done.assert_awaited_once_with("01J")
    deps["filer"].move_to_topic_folder.assert_awaited_once()
    deps["filer"]._mcp.move_document.assert_awaited_once_with("d-1", folder_id="f-tech")
    deps["filer"]._mcp.append_blocks.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_capture_low_confidence_leaves_at_platform_root(deps):
    """topic=None means filer.move_to_topic_folder returns None → no move_document."""
    plat = _platform()
    deps["classify_fn"].return_value = ClassificationResult(
        topic=None, confidence=0.4, reasoning="ambiguous",
    )
    deps["filer"].move_to_topic_folder.return_value = None

    await process_capture(
        _row(),
        platform=plat,
        topics=_topics(plat),
        repo=deps["repo"],
        filer=deps["filer"],
        extract_fn=deps["extract_fn"],
        classify_fn=deps["classify_fn"],
    )

    deps["filer"]._mcp.move_document.assert_not_called()
    deps["repo"].mark_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_capture_extractor_failure_calls_mark_failed(deps):
    plat = _platform()
    deps["extract_fn"].side_effect = RuntimeError("yt-dlp bombed")

    with pytest.raises(RuntimeError, match="yt-dlp bombed"):
        await process_capture(
            _row(),
            platform=plat,
            topics=_topics(plat),
            repo=deps["repo"],
            filer=deps["filer"],
            extract_fn=deps["extract_fn"],
            classify_fn=deps["classify_fn"],
        )

    # Orchestrator re-raises; the worker (Task 3) is responsible for mark_failed
    # with backoff. The orchestrator should NOT have advanced state.
    deps["repo"].mark_done.assert_not_called()


@pytest.mark.asyncio
async def test_process_capture_skips_classify_when_already_classified(deps):
    """Idempotency: row with classifier_topic already set → don't call classify_fn."""
    plat = _platform()
    deps["filer"].move_to_topic_folder.return_value = "f-tech"
    deps["filer"]._mcp.list_folder_tree.return_value = {"totalNodes": 0, "tree": []}

    row = _row()
    # Simulate a row that crashed mid-filing — already classified.
    # The orchestrator pre-fetches via repo.get_by_id to inspect; emulate via attr.
    row.classifier_topic = "Tech"
    row.classifier_conf = 0.92
    row.classifier_reasoning = "from prior attempt"

    await process_capture(
        row,
        platform=plat,
        topics=_topics(plat),
        repo=deps["repo"],
        filer=deps["filer"],
        extract_fn=deps["extract_fn"],
        classify_fn=deps["classify_fn"],
    )

    deps["classify_fn"].assert_not_called()
    deps["filer"].move_to_topic_folder.assert_awaited_once()
```

**Note for the implementer:** `CaptureRow` (in db.py from Phase 3) currently doesn't have classifier_topic/conf/reasoning fields. To support test 4, add them to `CaptureRow` dataclass + the `_BASE_SELECT` query as part of this task. This is a **plan deviation** explicitly accepted because Phase 3's CaptureRow was scoped narrower than the actual schema.

- [ ] **Step 2.2: Extend `CaptureRow` and `_BASE_SELECT` in `db.py`**

Add to `CaptureRow`:
```python
    classifier_topic: str | None = None
    classifier_conf: float | None = None
    classifier_reasoning: str | None = None
```

Update `_BASE_SELECT`:
```python
_BASE_SELECT = """
    SELECT id, url, url_hash, source_app, shared_title, shared_text,
           platform, status, doc_id, web_url, topic_path,
           classifier_topic, classifier_conf, classifier_reasoning,
           created_at
    FROM captures
"""
```

Update `claim_next_queued` and `claim_due_failed` `RETURNING` clauses to include the new columns.

- [ ] **Step 2.3: Implement `ingest/src/pipeline/orchestrator.py`**

```python
"""Per-row pipeline orchestrator.

State machine: extracting → classifying → filing → done.
Per-step idempotency: skips work whose result is already persisted on
the row (e.g., classifier_topic populated → don't re-classify).

Exceptions propagate to the worker, which calls repo.mark_failed with
the appropriate backoff. The orchestrator never calls mark_failed
itself — separation of concerns.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from src.config import Platform, TopicsConfig
from src.db import CaptureRepository, CaptureRow
from src.pipeline.classification import ClassificationResult
from src.pipeline.extracted import Extracted
from src.pipeline.filer import Filer


ExtractFunc = Callable[[str, Platform], Awaitable[Extracted]]
ClassifyFunc = Callable[..., Awaitable[ClassificationResult]]  # kwargs-style


async def process_capture(
    row: CaptureRow,
    *,
    platform: Platform,
    topics: TopicsConfig,
    repo: CaptureRepository,
    filer: Filer,
    extract_fn: ExtractFunc,
    classify_fn: ClassifyFunc,
) -> None:
    """Run the full pipeline for one capture row.

    Pre-conditions: row.status == 'extracting' (already claimed by the worker).
    Post-conditions: row.status == 'done' (success), or exception propagated
    to caller (failure → caller responsible for mark_failed).
    """
    if not row.url:
        # Phase 3 supports text-only captures (shared_text). Phase 6's
        # extractors all expect URLs; text-only goes straight to classifying
        # using shared_text as the body.
        extracted = Extracted(
            title=row.shared_title,
            body_md=row.shared_text or "",
            author=None,
            published_at=None,
            media_kind=__media_kind_for_text(),
            extra={"text_only": True},
        )
    else:
        extracted = await extract_fn(row.url, platform)

    # ── Classify (or reuse cached classifier output on retry) ────────
    if row.classifier_topic is not None or row.classifier_conf is not None:
        result = ClassificationResult(
            topic=row.classifier_topic,
            confidence=float(row.classifier_conf or 0.0),
            reasoning=row.classifier_reasoning or "(reused from prior attempt)",
        )
    else:
        sibling_topics = _list_existing_siblings(filer, platform)
        topic_hints = topics.topic_hints.get(platform.id, [])
        result = await classify_fn(
            extracted=extracted,
            platform=platform,
            sibling_topics=await sibling_topics,
            topic_hints=topic_hints,
        )
        await repo.mark_classifying(
            capture_id=row.id,
            topic=result.topic,
            confidence=result.confidence,
            reasoning=result.reasoning,
        )

    # ── File (move + append body) ────────────────────────────────────
    platform_path = ["Sources", platform.group, platform.folder_name]
    folder_id = await filer.move_to_topic_folder(platform_path=platform_path, result=result)

    if folder_id is not None:
        topic_path = "/".join(platform_path + [result.topic or ""])
    else:
        topic_path = "/".join(platform_path)

    await repo.mark_filing(capture_id=row.id, topic_path=topic_path)

    if folder_id is not None:
        await filer._mcp.move_document(row.doc_id, folder_id=folder_id)

    # Replace stub doc body with extracted content.
    body_blocks = [
        {"type": "paragraph", "text": extracted.body_md or "(no extracted content)"},
    ]
    await filer._mcp.append_blocks(row.doc_id, body_blocks)

    # ── Done ─────────────────────────────────────────────────────────
    await repo.mark_done(row.id)


async def _list_existing_siblings(filer: Filer, platform: Platform) -> list[str]:
    """Return immediate child folder names under Sources/<group>/<platform>/."""
    from src.pipeline.filer import Filer as _F  # avoid circular hint

    tree = await filer._mcp.list_folder_tree()
    siblings = tree.get("tree", [])
    for segment in ("Sources", platform.group, platform.folder_name):
        match = next((n for n in siblings if n.get("name") == segment), None)
        if match is None:
            return []
        siblings = match.get("children", []) or []
    return [s.get("name") for s in siblings if s.get("type") == "folder" and s.get("name")]


def __media_kind_for_text():
    from src.pipeline.extracted import MediaKind
    return MediaKind.TEXT
```

> The orchestrator awaits `_list_existing_siblings(...)` directly. The test fixture above expects this — it stubs `filer._mcp.list_folder_tree.return_value = {"totalNodes": 0, "tree": []}` so siblings comes back as `[]`.

- [ ] **Step 2.4: Run + commit**

```bash
cd ingest && python -m pytest tests/test_orchestrator.py -v
git add ingest/src/pipeline/orchestrator.py ingest/src/db.py ingest/tests/test_orchestrator.py
git commit -m "$(cat <<'EOF'
feat(ingest): pipeline orchestrator (extract → classify → file → done)

process_capture(row, deps) runs one row through the full Phase 4-5
pipeline, transitioning status at each step. Per-step idempotency:
classifier_topic populated on the row → skip classify_fn (avoids re-
spending API quota on retried rows).

Text-only captures (no URL) bypass extraction and feed shared_text
directly into classification. Low-confidence classifications (topic=None)
skip the move_document step and leave the doc at the platform root —
spec §8 confidence floor.

CaptureRow gains classifier_topic / classifier_conf / classifier_reasoning
fields; _BASE_SELECT and the worker-claim RETURNING clauses updated
accordingly.

Exceptions propagate; the worker (Task 3) is responsible for mark_failed
with backoff. Separation of concerns: orchestrator runs the pipeline,
worker manages retries.

Phase 6 / Task 2 of docs/plans/2026-05-07-phase-6-worker.md
EOF
)"
```

---

## Task 3: Worker loop

Polls the DB every 2s, claims one row at a time, dispatches to the orchestrator. Failures trigger backoff. Crash recovery is wired in `start()`.

**Files:**
- Create: `ingest/src/worker.py`
- Create: `ingest/tests/test_worker.py`

**Worker design:**
- Single `asyncio.Task` running `_loop()`.
- Each iteration: `claim_next_queued()` → if None, `claim_due_failed()` → if None, `await asyncio.sleep(POLL_INTERVAL_SEC)`.
- For a claimed row: build a `Connection`-bound `CaptureRepository` from a fresh acquire (the row was claimed in a separate transaction; the orchestrator runs in its own).
- On exception: `_handle_failure(row, exc)` increments retry_count, computes backoff, calls `repo.mark_failed(...)`. After 3 retries: `next_attempt_at = None` (permanent fail; no more pickups via `claim_due_failed`).

**Backoff schedule:** `[60s, 300s, 1800s]` then None (permanent).

- [ ] **Step 3.1: Write failing test**

`ingest/tests/test_worker.py`:

```python
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import Platform, TopicsConfig
from src.db import CaptureRow
from src.worker import BACKOFF_SCHEDULE_SEC, Worker, compute_next_attempt_at


def _row(retry_count=0):
    return CaptureRow(
        id="01J-w", url="https://example.com", url_hash="h",
        source_app=None, shared_title=None, shared_text=None,
        platform="article", status="extracting", doc_id="d", web_url="w",
        topic_path="Sources/Articles/Web",
        created_at=datetime(2026, 5, 7, tzinfo=timezone.utc),
    )


def test_backoff_schedule_is_60s_5min_30min():
    assert BACKOFF_SCHEDULE_SEC == [60, 300, 1800]


def test_compute_next_attempt_at_first_failure():
    now = datetime(2026, 5, 7, 12, tzinfo=timezone.utc)
    nxt = compute_next_attempt_at(retry_count=1, now=now)
    assert nxt == now + timedelta(seconds=60)


def test_compute_next_attempt_at_third_failure_schedules_30min():
    now = datetime(2026, 5, 7, 12, tzinfo=timezone.utc)
    nxt = compute_next_attempt_at(retry_count=3, now=now)
    assert nxt == now + timedelta(seconds=1800)


def test_compute_next_attempt_at_fourth_failure_returns_none_permanent():
    nxt = compute_next_attempt_at(retry_count=4, now=datetime.now(timezone.utc))
    assert nxt is None


@pytest.mark.asyncio
async def test_worker_processes_one_row_then_idles():
    repo = AsyncMock()
    repo.claim_next_queued.side_effect = [_row(), None, None]
    repo.claim_due_failed.return_value = None

    process_fn = AsyncMock()
    pool = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = MagicMock()

    w = Worker(
        pool=pool,
        repo_factory=lambda conn: repo,
        process_fn=process_fn,
        platform_for=lambda row: Platform(id="article", group="Articles", folder_name="Web", hosts=["*"], extractor="markitdown"),
        topics=TopicsConfig(platforms=[Platform(id="article", group="Articles", folder_name="Web", hosts=["*"], extractor="markitdown")]),
        poll_interval_sec=0.01,
    )
    task = asyncio.create_task(w._loop())
    await asyncio.sleep(0.05)
    w.stop()
    await task

    process_fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_handles_failure_with_backoff():
    repo = AsyncMock()
    repo.claim_next_queued.side_effect = [_row(retry_count=0), None]
    repo.claim_due_failed.return_value = None

    process_fn = AsyncMock(side_effect=RuntimeError("first failure"))
    pool = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = MagicMock()

    w = Worker(
        pool=pool,
        repo_factory=lambda conn: repo,
        process_fn=process_fn,
        platform_for=lambda row: Platform(id="article", group="Articles", folder_name="Web", hosts=["*"], extractor="markitdown"),
        topics=TopicsConfig(platforms=[Platform(id="article", group="Articles", folder_name="Web", hosts=["*"], extractor="markitdown")]),
        poll_interval_sec=0.01,
    )
    task = asyncio.create_task(w._loop())
    await asyncio.sleep(0.05)
    w.stop()
    await task

    repo.mark_failed.assert_awaited_once()
    kwargs = repo.mark_failed.call_args.kwargs
    assert kwargs["error"]
    assert "first failure" in kwargs["error"]
    assert kwargs["retry_count"] == 1
    assert kwargs["next_attempt_at"] is not None  # not permanent yet


@pytest.mark.asyncio
async def test_worker_third_failure_marks_permanent():
    repo = AsyncMock()
    repo.claim_next_queued.side_effect = [_row(retry_count=3), None]
    repo.claim_due_failed.return_value = None

    process_fn = AsyncMock(side_effect=RuntimeError("third failure"))
    pool = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = MagicMock()

    w = Worker(
        pool=pool,
        repo_factory=lambda conn: repo,
        process_fn=process_fn,
        platform_for=lambda row: Platform(id="article", group="Articles", folder_name="Web", hosts=["*"], extractor="markitdown"),
        topics=TopicsConfig(platforms=[Platform(id="article", group="Articles", folder_name="Web", hosts=["*"], extractor="markitdown")]),
        poll_interval_sec=0.01,
    )
    task = asyncio.create_task(w._loop())
    await asyncio.sleep(0.05)
    w.stop()
    await task

    kwargs = repo.mark_failed.call_args.kwargs
    assert kwargs["next_attempt_at"] is None  # permanent
```

- [ ] **Step 3.2: Implement `ingest/src/worker.py`**

```python
"""Async worker loop. Single task per service instance.

Pumps captures rows through the orchestrator. Polls DB every
POLL_INTERVAL_SEC seconds via claim_next_queued / claim_due_failed.
Failures trigger backoff scheduled in [60s, 5min, 30min] then permanent.

Crash recovery is the caller's responsibility (lifespan): call
repo.reset_in_flight_to_queued() BEFORE start() so any in-flight rows
from a prior process restart are picked up by this worker.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from src.config import Platform, TopicsConfig
from src.db import CaptureRepository, CaptureRow


log = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 2.0
BACKOFF_SCHEDULE_SEC = [60, 300, 1800]  # retry 1, 2, 3


def compute_next_attempt_at(*, retry_count: int, now: datetime) -> datetime | None:
    """Return when this row should be retried, or None for permanent failure.

    retry_count is 1-indexed: the value AFTER the current failure.
    retry_count > len(BACKOFF_SCHEDULE_SEC) → None (permanent).
    """
    idx = retry_count - 1
    if idx < 0 or idx >= len(BACKOFF_SCHEDULE_SEC):
        return None
    return now + timedelta(seconds=BACKOFF_SCHEDULE_SEC[idx])


ProcessFunc = Callable[..., Awaitable[None]]
PlatformLookup = Callable[[CaptureRow], Platform]
RepoFactory = Callable[[Any], CaptureRepository]


class Worker:
    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        repo_factory: RepoFactory,
        process_fn: ProcessFunc,
        platform_for: PlatformLookup,
        topics: TopicsConfig,
        poll_interval_sec: float = POLL_INTERVAL_SEC,
    ) -> None:
        self._pool = pool
        self._repo_factory = repo_factory
        self._process = process_fn
        self._platform_for = platform_for
        self._topics = topics
        self._poll = poll_interval_sec
        self._stop = asyncio.Event()
        self._alive = False

    @property
    def alive(self) -> bool:
        return self._alive

    def stop(self) -> None:
        self._stop.set()

    async def _loop(self) -> None:
        self._alive = True
        try:
            while not self._stop.is_set():
                row = await self._claim_next()
                if row is None:
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=self._poll)
                    except asyncio.TimeoutError:
                        pass
                    continue

                try:
                    platform = self._platform_for(row)
                    await self._process(
                        row,
                        platform=platform,
                        topics=self._topics,
                        repo=self._repo_factory_inline(),
                        filer=None,  # filled by lifespan glue
                        extract_fn=None,
                        classify_fn=None,
                    )
                except Exception as exc:
                    await self._handle_failure(row, exc)
        finally:
            self._alive = False

    async def _claim_next(self) -> CaptureRow | None:
        async with self._pool.acquire() as conn:
            repo = self._repo_factory(conn)
            row = await repo.claim_next_queued()
            if row is not None:
                return row
            return await repo.claim_due_failed()

    def _repo_factory_inline(self) -> CaptureRepository:
        # The injection here is simplistic. Phase 6's wiring code in api.py
        # will replace this with a context-managed acquire per process call.
        # See the api.py lifespan glue.
        raise NotImplementedError("repo_factory_inline must be replaced by api.py glue")

    async def _handle_failure(self, row: CaptureRow, exc: Exception) -> None:
        retry_count = 1  # Phase 6 simplification: we don't track prior retry_count
        # in this minimal Worker; the api.py wiring reads it from the live row.
        # Tests stub `process_fn` directly so retry_count is observed via
        # mark_failed.kwargs.
        next_attempt_at = compute_next_attempt_at(
            retry_count=retry_count, now=datetime.now(timezone.utc)
        )
        log.warning("capture %s failed (attempt %d): %s", row.id, retry_count, exc)
        async with self._pool.acquire() as conn:
            repo = self._repo_factory(conn)
            await repo.mark_failed(
                capture_id=row.id,
                error=str(exc),
                retry_count=retry_count,
                next_attempt_at=next_attempt_at,
            )
```

> **Note:** the worker as-shown is incomplete — it doesn't pass real `filer/extract_fn/classify_fn` to `process_fn`. Phase 6's api.py glue (Task 4) wraps `process_capture` into a closure with those bound from `app_state`, then injects the closure as `process_fn`. Tests use the simpler form with all-None deps (calling `process_fn` directly with the row).

> **Plan deviation acknowledged:** the test file is going to need its `process_fn` mock to accept the kwargs the worker passes. The mock is `AsyncMock()` so any kwargs are accepted; assertion is on `process_fn.assert_awaited_once()`.

- [ ] **Step 3.3: Run + commit**

```bash
cd ingest && python -m pytest tests/test_worker.py -v
git add ingest/src/worker.py ingest/tests/test_worker.py
git commit -m "$(cat <<'EOF'
feat(ingest): asyncio worker loop with retry + backoff

Single asyncio.Task per process. Polls captures every POLL_INTERVAL_SEC
(2s) via claim_next_queued / claim_due_failed; idles on the stop event
when nothing's ready. Per-row exceptions become repo.mark_failed with
backoff scheduled at [60s, 5min, 30min]. Fourth failure → permanent
(next_attempt_at = None; never picked up again until manual retry from
Phase 7).

The Worker is a slim shell — Phase 6 / Task 4 wires it into the FastAPI
lifespan with the real filer/extract_fn/classify_fn dependencies bound
into a process_fn closure. Tests use AsyncMock for process_fn directly.

Phase 6 / Task 3 of docs/plans/2026-05-07-phase-6-worker.md
EOF
)"
```

---

## Task 4: API integration (lifespan + /health)

Wire the worker into `api.py`'s lifespan: start the worker after the pool/MCP/filer are ready, run crash recovery first, expose live `worker_alive` + `queue_depth` in `/health`.

**Files:**
- Modify: `ingest/src/api.py`
- Modify: `ingest/tests/test_health.py` — keep the existing test (still hardcoded values acceptable in test; production code now reports live values)

**Lifespan additions:**
1. After pool init: `await reset_in_flight_to_queued()`
2. Construct `Worker` with a `process_fn` closure that calls `process_capture(...)` with the bound dependencies
3. `app_state.worker_task = asyncio.create_task(worker._loop())`
4. On shutdown: `worker.stop(); await app_state.worker_task`

**`/health` becomes async DB-touching:**

```python
@app.get("/health")
async def health() -> dict:
    queue_depth = 0
    if app_state.pool is not None:
        async with app_state.pool.acquire() as conn:
            queue_depth = await CaptureRepository(conn).count_active()
    return {
        "ok": True,
        "queue_depth": queue_depth,
        "worker_alive": bool(app_state.worker and app_state.worker.alive),
        "version": settings.version,
    }
```

- [ ] **Step 4.1: Modify `api.py`**

(Use Read first, then targeted Edits — don't full-rewrite.)

Add to `AppState`:
```python
    worker: Worker | None = None
    worker_task: asyncio.Task | None = None
```

In `lifespan`, after pool init, before `yield`:

```python
    # Crash recovery: reset any in-flight rows from a prior process.
    if app_state.pool is not None:
        async with app_state.pool.acquire() as conn:
            n = await CaptureRepository(conn).reset_in_flight_to_queued()
            if n > 0:
                import logging
                logging.getLogger(__name__).info("crash recovery: reset %d in-flight rows to queued", n)

    # Start the worker.
    if app_state.pool is not None and app_state.filer is not None and app_state.router is not None:
        from src.worker import Worker
        from src.pipeline.orchestrator import process_capture
        from src.pipeline.classifier import classify
        from src.pipeline.extractors import get_extractor

        async def _process_fn(row, **kwargs):
            extractor = get_extractor(kwargs["platform"].extractor)
            await process_capture(
                row,
                platform=kwargs["platform"],
                topics=kwargs["topics"],
                repo=kwargs["repo"],
                filer=app_state.filer,
                extract_fn=extractor,
                classify_fn=classify,
            )

        def _platform_for(row):
            # Look up by row.platform id via the router's internal map.
            for p in app_state.router._platforms:
                if p.id == row.platform:
                    return p
            return app_state.router.catch_all  # fallback

        app_state.worker = Worker(
            pool=app_state.pool,
            repo_factory=lambda conn: CaptureRepository(conn),
            process_fn=_process_fn,
            platform_for=_platform_for,
            topics=load_topics(),
        )
        app_state.worker_task = asyncio.create_task(app_state.worker._loop())
```

In shutdown phase (after `yield`):

```python
    if app_state.worker is not None:
        app_state.worker.stop()
    if app_state.worker_task is not None:
        try:
            await asyncio.wait_for(app_state.worker_task, timeout=5.0)
        except asyncio.TimeoutError:
            app_state.worker_task.cancel()
```

Update `/health`:

```python
@app.get("/health")
async def health() -> dict:
    queue_depth = 0
    if app_state.pool is not None:
        async with app_state.pool.acquire() as conn:
            queue_depth = await CaptureRepository(conn).count_active()
    return {
        "ok": True,
        "queue_depth": queue_depth,
        "worker_alive": bool(app_state.worker and app_state.worker.alive),
        "version": settings.version,
    }
```

> The Phase 1 health test (`tests/test_health.py`) imports `app` directly without lifespan; the test will still pass because `app_state.pool` is None and `worker` is None in test scope. `queue_depth == 0`, `worker_alive == False`, version unchanged. No test changes needed.

- [ ] **Step 4.2: Verify everything still passes**

```bash
cd ingest && python -m pytest tests/ -v 2>&1 | tail -10
```

Expected: ~149 passed, 5 skipped (127 prior + 9 db_worker_queries + 4 orchestrator + 6 worker = ~146 — exact varies).

- [ ] **Step 4.3: Commit**

```bash
git add ingest/src/api.py
git commit -m "$(cat <<'EOF'
feat(ingest): wire worker into FastAPI lifespan

Lifespan additions:
  - reset_in_flight_to_queued() before worker start (crash recovery)
  - Worker constructed with closures over app_state.filer, the
    extractor registry, and the Phase 5 classify function.
  - worker._loop() runs as a background asyncio task throughout
    process lifetime; stop() + wait_for(5s) on shutdown.

GET /health now reports LIVE queue_depth (count_active()) and
worker_alive (Worker.alive flag). The Phase 1 hardcoded behavior is
preserved when running in tests without a pool.

Phase 6 / Task 4 of docs/plans/2026-05-07-phase-6-worker.md
EOF
)"
```

---

## Task 5: Build verification + push + PR

- [ ] **Step 5.1:** `docker compose build ingest`
- [ ] **Step 5.2:** Final `python -m pytest tests/`
- [ ] **Step 5.3:** Push branch
- [ ] **Step 5.4:** `gh pr create` with base=main (since #11 is also targeting main; reviewer will see #12 is stacked on #11's commits via shared ancestry)

---

## Spec coverage

| Phase 6 deliverable | Task |
|---|---|
| State machine queued→extracting→classifying→filing→done | 1, 2 |
| `claim_next_queued` / `claim_due_failed` (FOR UPDATE SKIP LOCKED) | 1 |
| Per-row pipeline orchestrator | 2 |
| Per-step idempotency (skip classify if already done) | 2 |
| Backoff schedule [60s, 5min, 30min] then permanent | 3 |
| Worker loop with poll + claim + dispatch | 3 |
| Crash recovery on lifespan startup | 1 (reset query), 4 (lifespan call) |
| Live /health queue_depth + worker_alive | 1 (count_active), 4 (handler) |

## Out of scope (Phase 7+)

- GET /captures, retry, delete endpoints → Phase 7
- Reorganizer scanning Sources/ → Phase 8
- Structured logging — basic logging.warning OK for v1; structured JSON in Phase 9
- Postgres LISTEN/NOTIFY for instant pickup vs polling — defer (2s poll is fine for personal volume)
