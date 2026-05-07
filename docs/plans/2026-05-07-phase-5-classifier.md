# Phase 5 — Classification + Embedding-Similarity Folder Dedup

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Given an `Extracted` record, the classifier picks a topic via Anthropic Haiku 4.5 using sibling-folder context + per-platform topic hints. An embedding-similarity safety net prevents folder duplication (e.g., proposed `Cooking` collapses to existing `Recipes`). The filer then ensures the topic folder exists and moves the doc into it. Everything still runs at sync request time? **No** — Phase 5 only ships the *functions*; Phase 6's worker loop calls them.

**Macro plan:** [`docs/plans/2026-05-06-ingest-service-macro-plan.md`](./2026-05-06-ingest-service-macro-plan.md) — Phase 5
**Spec:** [`docs/specs/2026-05-06-ingest-service-design.md`](../specs/2026-05-06-ingest-service-design.md) — §8 (classification), §10 (folder_embeddings + topic_aliases tables)

**Architecture:**
1. `classifier.classify(extracted, platform, sibling_topics, topic_hints) -> ClassificationResult` — single Anthropic call with prompt caching on the system prompt; returns `{topic, confidence, reasoning, alias_of}`.
2. `embeddings.embed(text) -> list[float]` — OpenAI `text-embedding-3-small` (1536 dims).
3. `embeddings.find_similar_sibling(parent_path, name, threshold=0.85) -> existing_name | None` — cosine over the persisted `folder_embeddings` rows for that parent.
4. `Filer.move_to_topic_folder(doc_id, platform_folder_id, parent_path, topic) -> folder_id` — checks similarity → either reuses existing folder (and inserts a `topic_aliases` row) or creates the new folder + persists its embedding.
5. **Confidence floor**: `< 0.6` → leave doc at platform root (no topic folder), DB row keeps `needs_classification=true` for the reorganizer (Phase 8) to revisit.

**Tech Stack:**
- `anthropic>=0.40` Python SDK — Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) with prompt caching
- `openai>=1.40` (already added Phase 4) — `text-embedding-3-small`
- `pgvector` — already in postgres image; `<=>` cosine distance operator
- `numpy` for cosine fallback when comparing freshly-computed embeddings before persistence

**End-of-phase test count:** ~97 (current) + ~25 new unit + 2 integration (gated) ≈ ~125 passed, ~7 skipped.

---

## Task 1: Settings + topics.yaml hints + `ClassificationResult` model

**Files:**
- Modify: `ingest/pyproject.toml` — add `anthropic>=0.40`, `numpy>=1.26`
- Modify: `ingest/src/config.py` — add `anthropic_api_key`, `embedding_model`, `classifier_model`, `confidence_floor` to `Settings`
- Modify: `ingest/topics.yaml` — populate `topic_hints` per platform
- Create: `ingest/src/pipeline/classification.py` — `ClassificationResult` Pydantic model

- [ ] **Step 1.1: Update `pyproject.toml`**

```toml
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "asyncpg>=0.29",
    "pydantic>=2.7",
    "pydantic-settings>=2.5",
    "httpx>=0.27",
    "pyyaml>=6.0",
    "python-ulid>=2.4",
    "yt-dlp>=2025.1.0",
    "markitdown>=0.0.1",
    "openai>=1.40",
    "anthropic>=0.40",
    "numpy>=1.26",
]
```

- [ ] **Step 1.2: Extend `Settings`**

Add fields (preserve existing):
```python
    anthropic_api_key: str = ""
    classifier_model: str = "claude-haiku-4-5-20251001"
    embedding_model: str = "text-embedding-3-small"
    confidence_floor: float = 0.6
    similarity_threshold: float = 0.85
```

- [ ] **Step 1.3: Populate `topics.yaml` `topic_hints`**

Replace the empty `topic_hints: {}` block with seeded hints (per spec §8 example):

```yaml
# Per-platform topic vocabulary hint. The classifier MAY propose any topic
# but is biased toward this list when content matches. Add new topics here
# as you see them cluster at the platform root.
topic_hints:
  youtube:
    - Tutorials
    - Talks
    - Productivity
    - Programming
    - Music
    - Documentary
  instagram:
    - Recipes
    - Workouts
    - Travel
    - Architecture
    - Memes
    - Fashion
    - Tech
  tiktok:
    - Recipes
    - Memes
    - Workouts
    - Tutorials
    - Fashion
  x:
    - AI
    - Programming
    - Politics
    - Funny
    - Threads
    - Science
  reddit:
    - Programming
    - Tech
    - Cooking
    - Gaming
    - News
  arxiv:
    - Machine learning
    - Computer vision
    - NLP
    - Systems
    - Theory
  podcast_apple:
    - Tech
    - Business
    - Science
    - Comedy
    - News
  spotify_episode:
    - Tech
    - Business
    - Science
    - Comedy
    - News
  article:
    - Tech
    - Science
    - Business
    - Culture
    - Health
    - Politics
```

- [ ] **Step 1.4: Write failing test for `ClassificationResult`**

Create `ingest/tests/test_classification_model.py`:

```python
import pytest
from pydantic import ValidationError

from src.pipeline.classification import ClassificationResult


def test_classification_result_minimal():
    r = ClassificationResult(topic="Recipes", confidence=0.92, reasoning="dish photo")
    assert r.topic == "Recipes"
    assert r.confidence == 0.92
    assert r.alias_of is None


def test_classification_result_with_alias():
    r = ClassificationResult(topic="Cooking", confidence=0.85, reasoning="similar to Recipes", alias_of="Recipes")
    assert r.alias_of == "Recipes"


def test_classification_result_low_confidence_topic_can_be_null():
    r = ClassificationResult(topic=None, confidence=0.3, reasoning="ambiguous content")
    assert r.topic is None


def test_classification_result_rejects_confidence_out_of_range():
    with pytest.raises(ValidationError):
        ClassificationResult(topic="X", confidence=1.5, reasoning="bug")
    with pytest.raises(ValidationError):
        ClassificationResult(topic="X", confidence=-0.1, reasoning="bug")


def test_classification_result_strips_topic_whitespace():
    r = ClassificationResult(topic="  Recipes  ", confidence=0.9, reasoning="x")
    assert r.topic == "Recipes"


def test_classification_result_empty_topic_becomes_none():
    r = ClassificationResult(topic="", confidence=0.5, reasoning="x")
    assert r.topic is None
```

- [ ] **Step 1.5: Implement `ingest/src/pipeline/classification.py`**

```python
"""Wire types for the classifier output."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ClassificationResult(BaseModel):
    """What the classifier returns for one Extracted record.

    `topic` may be None when the classifier is not confident enough to assign
    one; in that case `alias_of` is also None and the doc lands at the
    platform root (Phase 8 reorganizer revisits).

    `alias_of` lets the model propose a topic name that should collapse to
    an existing sibling (e.g., topic="Cooking", alias_of="Recipes") without
    creating a duplicate folder. The filer respects this directly without
    a separate embedding lookup.
    """

    model_config = ConfigDict(extra="ignore")

    topic: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    alias_of: str | None = None

    @field_validator("topic", mode="before")
    @classmethod
    def _normalize_topic(cls, value):
        if value is None:
            return None
        s = str(value).strip()
        return s if s else None
```

- [ ] **Step 1.6: Install + run tests**

```bash
cd ingest && pip install -e ".[dev]" && python -m pytest tests/test_classification_model.py -v
```

Expected: 6 passed.

- [ ] **Step 1.7: Commit**

```bash
git add ingest/pyproject.toml ingest/src/config.py ingest/topics.yaml ingest/src/pipeline/classification.py ingest/tests/test_classification_model.py
git commit -m "$(cat <<'EOF'
feat(ingest): classifier wire types + topic hints + settings

Adds ClassificationResult Pydantic model — what the classifier returns
for one extraction (topic, confidence 0..1, reasoning, alias_of).
Empty-string and whitespace-only topics normalize to None.

Settings gains anthropic_api_key, classifier_model
(claude-haiku-4-5-20251001), embedding_model (text-embedding-3-small),
confidence_floor (0.6), similarity_threshold (0.85). Runtime deps add
anthropic + numpy.

topics.yaml topic_hints populated per platform — the classifier biases
toward these but isn't bound to them. Add new topics as patterns
cluster at the platform root.

Phase 5 / Task 1 of docs/plans/2026-05-07-phase-5-classifier.md
EOF
)"
```

---

## Task 2: Embeddings module (OpenAI + cosine)

Single function `embed(text)` calls OpenAI; helper `cosine(a, b)` computes similarity. Pure functions; no DB yet.

**Files:**
- Create: `ingest/src/pipeline/embeddings.py`
- Create: `ingest/tests/test_embeddings.py`

- [ ] **Step 2.1: Write failing tests**

```python
import math
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from src.pipeline.embeddings import cosine_similarity, embed


def test_cosine_similarity_identical_vectors_is_one():
    a = [0.1, 0.2, 0.3]
    assert math.isclose(cosine_similarity(a, a), 1.0, abs_tol=1e-9)


def test_cosine_similarity_orthogonal_is_zero():
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert math.isclose(cosine_similarity(a, b), 0.0, abs_tol=1e-9)


def test_cosine_similarity_opposite_is_minus_one():
    a = [1.0, 0.0]
    b = [-1.0, 0.0]
    assert math.isclose(cosine_similarity(a, b), -1.0, abs_tol=1e-9)


def test_cosine_similarity_zero_vector_returns_zero():
    """Avoid div-by-zero; an undefined cosine is treated as 0 (no similarity)."""
    a = [0.0, 0.0, 0.0]
    b = [1.0, 0.0, 0.0]
    assert cosine_similarity(a, b) == 0.0


@pytest.mark.asyncio
async def test_embed_calls_openai_with_correct_args():
    fake_response = MagicMock()
    fake_response.data = [MagicMock(embedding=[0.1, 0.2, 0.3])]

    with patch("src.pipeline.embeddings.AsyncOpenAI") as Client:
        instance = Client.return_value
        instance.embeddings.create = AsyncMock(return_value=fake_response)

        vec = await embed("Recipes")

    Client.assert_called_once()  # constructed with api_key
    instance.embeddings.create.assert_awaited_once()
    call = instance.embeddings.create.await_args
    assert call.kwargs["model"] == "text-embedding-3-small"
    assert call.kwargs["input"] == "Recipes"
    assert vec == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_embed_raises_when_api_key_missing(monkeypatch):
    monkeypatch.setattr("src.pipeline.embeddings.settings", MagicMock(openai_api_key="", embedding_model="x"))
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        await embed("anything")
```

- [ ] **Step 2.2: Implement `ingest/src/pipeline/embeddings.py`**

```python
"""OpenAI embeddings + cosine similarity helpers.

`embed(text)` calls OpenAI text-embedding-3-small (1536 dims).
`cosine_similarity(a, b)` is a tiny numpy implementation used to compare
freshly-computed embeddings before persistence (the production lookup
goes through pgvector's <=> operator — see db.py).
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from openai import AsyncOpenAI

from src.config import settings


async def embed(text: str) -> list[float]:
    """Return the embedding vector for `text`. Raises if OPENAI_API_KEY missing."""
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not set; cannot compute embedding")
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    result = await client.embeddings.create(
        model=settings.embedding_model,
        input=text,
    )
    return list(result.data[0].embedding)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity in [-1, 1]. Returns 0.0 when either vector is zero."""
    va = np.asarray(a, dtype=np.float32)
    vb = np.asarray(b, dtype=np.float32)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denom == 0.0:
        return 0.0
    return float(np.dot(va, vb) / denom)
```

- [ ] **Step 2.3: Run, see pass; commit**

```bash
cd ingest && python -m pytest tests/test_embeddings.py -v
git add ingest/src/pipeline/embeddings.py ingest/tests/test_embeddings.py
git commit -m "$(cat <<'EOF'
feat(ingest): embedding helpers (OpenAI + cosine similarity)

embed(text) calls OpenAI text-embedding-3-small returning a 1536-dim
list[float]. cosine_similarity(a, b) is a numpy-backed pure function
returning [-1, 1], with zero-vector div-by-zero hardened to 0.0.

Used by Phase 5 Task 5 to dedup proposed folder names against existing
siblings (e.g., proposed "Cooking" collapses to existing "Recipes" when
cosine > similarity_threshold=0.85). Production folder lookup goes
through pgvector's <=> in DB; this in-Python helper compares
freshly-computed pairs before persistence.

Phase 5 / Task 2 of docs/plans/2026-05-07-phase-5-classifier.md
EOF
)"
```

---

## Task 3: DB queries for folder_embeddings + topic_aliases

Extend `db.py` with two new repos: `FolderEmbeddingRepository` and `TopicAliasRepository`.

**Files:**
- Modify: `ingest/src/db.py` — add two new dataclasses + repos
- Create: `ingest/tests/test_db_phase5.py`

- [ ] **Step 3.1: Write failing test**

```python
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.db import (
    FolderEmbeddingRepository,
    FolderEmbeddingRow,
    TopicAliasRepository,
)


@pytest.mark.asyncio
async def test_folder_embedding_upsert_executes_correct_sql():
    conn = AsyncMock()
    repo = FolderEmbeddingRepository(conn)
    await repo.upsert(FolderEmbeddingRow(
        folder_id="f1",
        folder_name="Recipes",
        parent_path="Sources/Socials/Instagram",
        embedding=[0.1, 0.2, 0.3],
    ))
    sql, *args = conn.execute.call_args.args
    assert "INSERT INTO folder_embeddings" in sql
    assert "ON CONFLICT" in sql
    assert args[0] == "f1"
    assert args[1] == "Recipes"
    assert args[2] == "Sources/Socials/Instagram"


@pytest.mark.asyncio
async def test_folder_embedding_list_for_parent_returns_rows():
    conn = AsyncMock()
    conn.fetch.return_value = [
        {"folder_id": "f1", "folder_name": "Recipes",
         "parent_path": "Sources/Socials/Instagram",
         "embedding": "[0.1,0.2,0.3]",
         "updated_at": datetime(2026, 5, 7, tzinfo=timezone.utc)},
    ]
    repo = FolderEmbeddingRepository(conn)
    rows = await repo.list_for_parent("Sources/Socials/Instagram")
    assert len(rows) == 1
    assert rows[0].folder_name == "Recipes"
    assert rows[0].embedding == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_topic_alias_record_executes_insert():
    conn = AsyncMock()
    repo = TopicAliasRepository(conn)
    await repo.record(parent_path="Sources/Socials/Instagram", alias="Cooking", canonical="Recipes")
    sql, *args = conn.execute.call_args.args
    assert "INSERT INTO topic_aliases" in sql
    assert "ON CONFLICT" in sql
    assert args == ("Sources/Socials/Instagram", "Cooking", "Recipes")


@pytest.mark.asyncio
async def test_topic_alias_lookup_returns_canonical_when_present():
    conn = AsyncMock()
    conn.fetchval.return_value = "Recipes"
    repo = TopicAliasRepository(conn)
    canonical = await repo.lookup(parent_path="Sources/Socials/Instagram", alias="Cooking")
    assert canonical == "Recipes"


@pytest.mark.asyncio
async def test_topic_alias_lookup_returns_none_when_absent():
    conn = AsyncMock()
    conn.fetchval.return_value = None
    repo = TopicAliasRepository(conn)
    assert await repo.lookup(parent_path="x", alias="y") is None
```

- [ ] **Step 3.2: Implement — append to `ingest/src/db.py`**

Append below the existing `CaptureRepository`:

```python
# ── Folder embeddings + topic aliases (Phase 5) ───────────────────────


@dataclass
class FolderEmbeddingRow:
    folder_id: str
    folder_name: str
    parent_path: str
    embedding: list[float]


_FOLDER_EMBEDDING_UPSERT_SQL = """
    INSERT INTO folder_embeddings (folder_id, folder_name, parent_path, embedding, updated_at)
    VALUES ($1, $2, $3, $4, NOW())
    ON CONFLICT (folder_id) DO UPDATE
        SET folder_name = EXCLUDED.folder_name,
            parent_path = EXCLUDED.parent_path,
            embedding   = EXCLUDED.embedding,
            updated_at  = NOW()
"""

_FOLDER_EMBEDDING_LIST_SQL = """
    SELECT folder_id, folder_name, parent_path, embedding, updated_at
    FROM folder_embeddings
    WHERE parent_path = $1
"""


def _format_pgvector(vec: list[float]) -> str:
    """pgvector's text representation: '[v1,v2,...]' with no spaces."""
    return "[" + ",".join(f"{v:.7g}" for v in vec) + "]"


def _parse_pgvector(s: str | list[float]) -> list[float]:
    """Parse pgvector text or pass through if asyncpg returned a list."""
    if isinstance(s, list):
        return [float(v) for v in s]
    s = s.strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [float(x) for x in s.split(",") if x]


class FolderEmbeddingRepository:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def upsert(self, row: FolderEmbeddingRow) -> None:
        await self._conn.execute(
            _FOLDER_EMBEDDING_UPSERT_SQL,
            row.folder_id,
            row.folder_name,
            row.parent_path,
            _format_pgvector(row.embedding),
        )

    async def list_for_parent(self, parent_path: str) -> list[FolderEmbeddingRow]:
        records = await self._conn.fetch(_FOLDER_EMBEDDING_LIST_SQL, parent_path)
        return [
            FolderEmbeddingRow(
                folder_id=r["folder_id"],
                folder_name=r["folder_name"],
                parent_path=r["parent_path"],
                embedding=_parse_pgvector(r["embedding"]),
            )
            for r in records
        ]


_TOPIC_ALIAS_UPSERT_SQL = """
    INSERT INTO topic_aliases (parent_path, alias, canonical)
    VALUES ($1, $2, $3)
    ON CONFLICT (parent_path, alias) DO UPDATE
        SET canonical = EXCLUDED.canonical
"""

_TOPIC_ALIAS_LOOKUP_SQL = """
    SELECT canonical FROM topic_aliases WHERE parent_path = $1 AND alias = $2
"""


class TopicAliasRepository:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def record(self, *, parent_path: str, alias: str, canonical: str) -> None:
        await self._conn.execute(_TOPIC_ALIAS_UPSERT_SQL, parent_path, alias, canonical)

    async def lookup(self, *, parent_path: str, alias: str) -> str | None:
        return await self._conn.fetchval(_TOPIC_ALIAS_LOOKUP_SQL, parent_path, alias)
```

- [ ] **Step 3.3: Run + commit**

```bash
cd ingest && python -m pytest tests/test_db_phase5.py -v
git add ingest/src/db.py ingest/tests/test_db_phase5.py
git commit -m "$(cat <<'EOF'
feat(ingest): folder_embeddings + topic_aliases repos

FolderEmbeddingRepository.upsert / .list_for_parent operates on pgvector
text format ([v1,v2,...]) so we don't depend on asyncpg-pgvector codec.
TopicAliasRepository.record / .lookup persists alias→canonical mappings
that bypass the embedding lookup on subsequent classifications.

Both repos are AsyncMock-tested for SQL shape + parameter binding.

Phase 5 / Task 3 of docs/plans/2026-05-07-phase-5-classifier.md
EOF
)"
```

---

## Task 4: Classifier (Anthropic Haiku 4.5 with prompt caching)

Single async function `classify(extracted, platform, sibling_topics, topic_hints) -> ClassificationResult`. Builds a system prompt (cached) + user message (not cached) and parses the structured JSON response.

**Files:**
- Create: `ingest/src/pipeline/classifier.py`
- Create: `ingest/tests/test_classifier.py`
- Create: `ingest/tests/fixtures/classifier_prompt_golden.txt`

- [ ] **Step 4.1: Write failing test**

```python
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import Platform
from src.pipeline.classification import ClassificationResult
from src.pipeline.classifier import build_user_message, classify, SYSTEM_PROMPT
from src.pipeline.extracted import Extracted, MediaKind


def _platform() -> Platform:
    return Platform(id="instagram", group="Socials", folder_name="Instagram",
                    hosts=["instagram.com"], extractor="ytdlp")


def _extracted(body: str = "Honey-glazed salmon recipe with photos.") -> Extracted:
    return Extracted(
        title="Honey-glazed salmon",
        body_md=body,
        author="@cookingchannel",
        published_at=None,
        media_kind=MediaKind.IMAGE,
        extra={},
    )


def test_system_prompt_contains_required_instructions():
    """The system prompt must explain JSON output, alias_of semantics, and confidence range."""
    assert "JSON" in SYSTEM_PROMPT
    assert "alias_of" in SYSTEM_PROMPT
    assert "confidence" in SYSTEM_PROMPT.lower()


def test_build_user_message_includes_siblings_and_hints():
    msg = build_user_message(
        extracted=_extracted(),
        platform=_platform(),
        sibling_topics=["Recipes", "Workouts"],
        topic_hints=["Recipes", "Workouts", "Travel", "Architecture", "Memes"],
    )
    assert "instagram" in msg.lower()
    assert "Recipes" in msg
    assert "Workouts" in msg
    assert "Honey-glazed salmon" in msg


def test_build_user_message_truncates_long_body():
    long_body = "X" * 30_000
    msg = build_user_message(
        extracted=_extracted(body=long_body),
        platform=_platform(),
        sibling_topics=[],
        topic_hints=[],
    )
    # Body region should be capped well below 30k.
    assert len(msg) < 15_000


def test_user_message_golden(tmp_path: Path):
    """Compare the assembled prompt against a checked-in golden file."""
    golden = (Path(__file__).parent / "fixtures" / "classifier_prompt_golden.txt").read_text(encoding="utf-8")
    msg = build_user_message(
        extracted=Extracted(
            title="Honey-glazed salmon",
            body_md="Recipe with ingredients and steps.",
            author="@cookingchannel",
            published_at=None,
            media_kind=MediaKind.IMAGE,
            extra={},
        ),
        platform=_platform(),
        sibling_topics=["Recipes", "Workouts"],
        topic_hints=["Recipes", "Workouts", "Travel"],
    )
    assert msg == golden


@pytest.mark.asyncio
async def test_classify_parses_anthropic_json_response():
    fake_response = MagicMock()
    fake_response.content = [MagicMock(text=json.dumps({
        "topic": "Recipes",
        "confidence": 0.92,
        "reasoning": "Caption lists ingredients; image shows plated dish.",
        "alias_of": None,
    }))]

    with patch("src.pipeline.classifier.AsyncAnthropic") as Client:
        Client.return_value.messages.create = AsyncMock(return_value=fake_response)
        result = await classify(
            extracted=_extracted(),
            platform=_platform(),
            sibling_topics=["Recipes"],
            topic_hints=["Recipes", "Workouts"],
        )

    assert isinstance(result, ClassificationResult)
    assert result.topic == "Recipes"
    assert result.confidence == 0.92
    assert result.alias_of is None


@pytest.mark.asyncio
async def test_classify_handles_alias_of_response():
    fake_response = MagicMock()
    fake_response.content = [MagicMock(text=json.dumps({
        "topic": "Cooking",
        "confidence": 0.88,
        "reasoning": "Recipe content; existing 'Recipes' folder fits.",
        "alias_of": "Recipes",
    }))]

    with patch("src.pipeline.classifier.AsyncAnthropic") as Client:
        Client.return_value.messages.create = AsyncMock(return_value=fake_response)
        result = await classify(
            extracted=_extracted(),
            platform=_platform(),
            sibling_topics=["Recipes"],
            topic_hints=["Recipes"],
        )
    assert result.topic == "Cooking"
    assert result.alias_of == "Recipes"


@pytest.mark.asyncio
async def test_classify_low_confidence_with_null_topic():
    fake_response = MagicMock()
    fake_response.content = [MagicMock(text=json.dumps({
        "topic": None,
        "confidence": 0.4,
        "reasoning": "Content is ambiguous between Recipes and Memes.",
        "alias_of": None,
    }))]

    with patch("src.pipeline.classifier.AsyncAnthropic") as Client:
        Client.return_value.messages.create = AsyncMock(return_value=fake_response)
        result = await classify(
            extracted=_extracted(),
            platform=_platform(),
            sibling_topics=[],
            topic_hints=[],
        )
    assert result.topic is None
    assert result.confidence == 0.4


@pytest.mark.asyncio
async def test_classify_uses_system_prompt_caching():
    fake_response = MagicMock()
    fake_response.content = [MagicMock(text=json.dumps({
        "topic": "Recipes", "confidence": 0.9, "reasoning": "x", "alias_of": None,
    }))]
    with patch("src.pipeline.classifier.AsyncAnthropic") as Client:
        instance = Client.return_value
        instance.messages.create = AsyncMock(return_value=fake_response)
        await classify(
            extracted=_extracted(),
            platform=_platform(),
            sibling_topics=[],
            topic_hints=[],
        )
    call = instance.messages.create.await_args
    system = call.kwargs["system"]
    # System is a list of blocks (cache_control attached) per Anthropic SDK shape
    assert isinstance(system, list)
    assert system[0]["type"] == "text"
    assert system[0].get("cache_control") == {"type": "ephemeral"}
```

- [ ] **Step 4.2: Create the golden file**

`ingest/tests/fixtures/classifier_prompt_golden.txt` (content depends on the exact `build_user_message` formatting; write the impl first, then capture its output once and save).

For the plan: produce this template text. The implementer should run the test, capture the actual `msg` string, write it to the file, and re-run.

```
Platform: instagram (Socials/Instagram)

Existing topic folders under Sources/Socials/Instagram/:
- Recipes
- Workouts

Suggested topics for this platform (you may propose others):
Recipes, Workouts, Travel

Captured content:
- Title: Honey-glazed salmon
- Author: @cookingchannel
- Media kind: image

Body (truncated to first 8000 chars):

Recipe with ingredients and steps.
```

(The implementer should use `build_user_message(...)`'s actual output — the test will fail if the implementation drifts, which is the point of a golden test.)

- [ ] **Step 4.3: Implement `ingest/src/pipeline/classifier.py`**

```python
"""Anthropic Haiku 4.5 classifier with prompt caching.

Single call per Extracted record. The system prompt explains the JSON
contract, the alias_of mechanism, and the confidence floor; it's marked
with cache_control: ephemeral so subsequent calls reuse the prefix.

The user message is fresh per call: platform, existing siblings, topic
hints, and the captured content excerpt.
"""

from __future__ import annotations

import json
from typing import Any

from anthropic import AsyncAnthropic

from src.config import Platform, settings
from src.pipeline.classification import ClassificationResult
from src.pipeline.extracted import Extracted


SYSTEM_PROMPT = """You are a content classifier for a personal knowledge base.
Your job is to assign a topic folder to a captured social-media or web post.

You will be given:
- The platform (e.g., Instagram, YouTube, X, arXiv)
- The list of existing topic folders under that platform
- A list of suggested topics for that platform (a hint, not a constraint)
- The captured content (title, author, media kind, body excerpt)

Output strict JSON with exactly these keys:
{
  "topic": string | null,        // The chosen topic name. null if confidence < 0.6.
  "confidence": number,          // 0.0 to 1.0.
  "reasoning": string,           // 1-2 sentences. What in the content drove the choice.
  "alias_of": string | null      // If your proposed topic is a duplicate of an existing
                                  // sibling (e.g., you propose "Cooking" and "Recipes"
                                  // already exists), set alias_of to the existing name.
}

Guidelines:
- PREFER reusing an existing sibling topic when content fits.
- Only propose a NEW topic when content is clearly distinct from all siblings.
- If the proposed new topic is semantically similar to an existing sibling
  (e.g., Cooking vs Recipes, Workouts vs Fitness), set alias_of to the
  existing name and the system will collapse them.
- Keep topic names short (1-2 words), Title Case.
- Confidence below 0.6 means topic should be null and the doc stays
  unfiled at the platform root for later review.

Return ONLY the JSON object. No prose, no markdown fences.
"""


def build_user_message(
    *,
    extracted: Extracted,
    platform: Platform,
    sibling_topics: list[str],
    topic_hints: list[str],
) -> str:
    siblings_block = (
        "\n".join(f"- {s}" for s in sibling_topics)
        if sibling_topics
        else "(none — this would be the first topic folder under this platform)"
    )
    hints_block = ", ".join(topic_hints) if topic_hints else "(no hints configured)"
    body_excerpt = (extracted.body_md or "")[:8000]

    return (
        f"Platform: {platform.id} ({platform.group}/{platform.folder_name})\n"
        f"\n"
        f"Existing topic folders under Sources/{platform.group}/{platform.folder_name}/:\n"
        f"{siblings_block}\n"
        f"\n"
        f"Suggested topics for this platform (you may propose others):\n"
        f"{hints_block}\n"
        f"\n"
        f"Captured content:\n"
        f"- Title: {extracted.title or '(none)'}\n"
        f"- Author: {extracted.author or '(unknown)'}\n"
        f"- Media kind: {extracted.media_kind.value}\n"
        f"\n"
        f"Body (truncated to first 8000 chars):\n"
        f"\n"
        f"{body_excerpt}\n"
    )


async def classify(
    *,
    extracted: Extracted,
    platform: Platform,
    sibling_topics: list[str],
    topic_hints: list[str],
) -> ClassificationResult:
    """Single Anthropic call → ClassificationResult."""
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    user_msg = build_user_message(
        extracted=extracted,
        platform=platform,
        sibling_topics=sibling_topics,
        topic_hints=topic_hints,
    )

    response = await client.messages.create(
        model=settings.classifier_model,
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_msg}],
    )

    text = response.content[0].text.strip()
    # Strip optional code-fence if the model wrapped output.
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    payload: dict[str, Any] = json.loads(text)

    return ClassificationResult.model_validate(payload)
```

- [ ] **Step 4.4: Generate the golden file**

After implementing, run:
```bash
cd ingest && python -c "
from src.config import Platform
from src.pipeline.classifier import build_user_message
from src.pipeline.extracted import Extracted, MediaKind

msg = build_user_message(
    extracted=Extracted(
        title='Honey-glazed salmon',
        body_md='Recipe with ingredients and steps.',
        author='@cookingchannel',
        published_at=None,
        media_kind=MediaKind.IMAGE,
        extra={},
    ),
    platform=Platform(id='instagram', group='Socials', folder_name='Instagram',
                      hosts=['instagram.com'], extractor='ytdlp'),
    sibling_topics=['Recipes', 'Workouts'],
    topic_hints=['Recipes', 'Workouts', 'Travel'],
)
import sys
sys.stdout.write(msg)
" > tests/fixtures/classifier_prompt_golden.txt
```

Verify the file looks right (open and inspect), then run the test which compares against it.

- [ ] **Step 4.5: Run all tests + commit**

```bash
cd ingest && python -m pytest tests/test_classifier.py -v
git add ingest/src/pipeline/classifier.py ingest/tests/test_classifier.py ingest/tests/fixtures/classifier_prompt_golden.txt
git commit -m "$(cat <<'EOF'
feat(ingest): Anthropic Haiku 4.5 classifier with prompt caching

classify(extracted, platform, sibling_topics, topic_hints) returns
ClassificationResult. System prompt (instruction set + JSON contract)
is marked cache_control: ephemeral — subsequent calls reuse the prefix.

User message includes platform identity, existing siblings, topic hints,
and the first 8000 chars of body_md. Output is strict JSON; the parser
strips optional code-fence wrappers if the model adds them.

Tested with mocked AsyncAnthropic for happy path, alias_of, low-
confidence-null-topic, and prompt-cache-block presence. Golden file
guards against drift in the user message format.

Phase 5 / Task 4 of docs/plans/2026-05-07-phase-5-classifier.md
EOF
)"
```

---

## Task 5: Filer integration — `move_to_topic_folder` with similarity dedup

Wire the classifier output to actual folder placement. Five-step flow:

1. Lookup `topic_aliases` for the parent path. If alias exists, use canonical.
2. If `alias_of` is set in the result, use that directly (skip embedding check).
3. Else, embed the proposed topic, list `folder_embeddings` for the parent, find best match. If `cosine > similarity_threshold`, use that existing folder; record an alias.
4. Else, create a new folder under the parent + embed its name + persist in `folder_embeddings`.
5. Return the chosen folder id; caller uses it to `move_document`.

**Files:**
- Modify: `ingest/src/pipeline/filer.py` — add `move_to_topic_folder` method
- Create: `ingest/tests/test_filer_topic_routing.py`

- [ ] **Step 5.1: Write failing test (key cases)**

```python
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.db import FolderEmbeddingRow
from src.pipeline.classification import ClassificationResult
from src.pipeline.filer import Filer


def _make_filer():
    mcp = AsyncMock()
    mcp.list_folder_tree.return_value = {"totalNodes": 0, "tree": [
        {"id": "f-sources", "type": "folder", "name": "Sources", "children": [
            {"id": "f-socials", "type": "folder", "name": "Socials", "children": [
                {"id": "f-ig", "type": "folder", "name": "Instagram", "children": [
                    {"id": "f-recipes", "type": "folder", "name": "Recipes", "children": []},
                ]},
            ]},
        ]},
    ]}
    embeddings_repo = AsyncMock()
    aliases_repo = AsyncMock()
    embed_fn = AsyncMock(return_value=[0.1] * 1536)
    return Filer(mcp, embeddings_repo=embeddings_repo, aliases_repo=aliases_repo, embed_fn=embed_fn), mcp, embeddings_repo, aliases_repo, embed_fn


@pytest.mark.asyncio
async def test_move_to_topic_uses_existing_alias_first():
    filer, mcp, embeddings_repo, aliases_repo, embed_fn = _make_filer()
    aliases_repo.lookup.return_value = "Recipes"  # existing alias
    embeddings_repo.list_for_parent.return_value = [
        FolderEmbeddingRow(folder_id="f-recipes", folder_name="Recipes",
                           parent_path="Sources/Socials/Instagram",
                           embedding=[0.1] * 1536),
    ]

    result = ClassificationResult(topic="Cooking", confidence=0.9, reasoning="x")
    folder_id = await filer.move_to_topic_folder(
        platform_path=["Sources", "Socials", "Instagram"],
        result=result,
    )

    assert folder_id == "f-recipes"
    aliases_repo.lookup.assert_awaited_once()
    # Alias hit short-circuits embedding work.
    embed_fn.assert_not_called()
    # No new folder created.
    mcp.create_folder.assert_not_called()


@pytest.mark.asyncio
async def test_move_to_topic_explicit_alias_of_skips_embedding():
    filer, mcp, embeddings_repo, aliases_repo, embed_fn = _make_filer()
    aliases_repo.lookup.return_value = None
    embeddings_repo.list_for_parent.return_value = [
        FolderEmbeddingRow(folder_id="f-recipes", folder_name="Recipes",
                           parent_path="Sources/Socials/Instagram",
                           embedding=[0.1] * 1536),
    ]

    result = ClassificationResult(topic="Cooking", confidence=0.9, reasoning="x", alias_of="Recipes")
    folder_id = await filer.move_to_topic_folder(
        platform_path=["Sources", "Socials", "Instagram"],
        result=result,
    )

    assert folder_id == "f-recipes"
    embed_fn.assert_not_called()
    aliases_repo.record.assert_awaited_once()
    args = aliases_repo.record.call_args.kwargs
    assert args["alias"] == "Cooking"
    assert args["canonical"] == "Recipes"


@pytest.mark.asyncio
async def test_move_to_topic_high_similarity_collapses():
    """Proposed 'Cooking' with embedding cosine > 0.85 vs existing 'Recipes' → collapse."""
    filer, mcp, embeddings_repo, aliases_repo, embed_fn = _make_filer()
    aliases_repo.lookup.return_value = None
    embeddings_repo.list_for_parent.return_value = [
        FolderEmbeddingRow(folder_id="f-recipes", folder_name="Recipes",
                           parent_path="Sources/Socials/Instagram",
                           embedding=[1.0, 0.0, 0.0] + [0.0] * 1533),
    ]
    embed_fn.return_value = [0.99, 0.01, 0.0] + [0.0] * 1533  # cosine ≈ 0.9999

    result = ClassificationResult(topic="Cooking", confidence=0.9, reasoning="x")
    folder_id = await filer.move_to_topic_folder(
        platform_path=["Sources", "Socials", "Instagram"],
        result=result,
    )

    assert folder_id == "f-recipes"
    aliases_repo.record.assert_awaited_once()
    mcp.create_folder.assert_not_called()


@pytest.mark.asyncio
async def test_move_to_topic_low_similarity_creates_new():
    filer, mcp, embeddings_repo, aliases_repo, embed_fn = _make_filer()
    aliases_repo.lookup.return_value = None
    embeddings_repo.list_for_parent.return_value = [
        FolderEmbeddingRow(folder_id="f-recipes", folder_name="Recipes",
                           parent_path="Sources/Socials/Instagram",
                           embedding=[1.0, 0.0] + [0.0] * 1534),
    ]
    embed_fn.return_value = [0.0, 1.0] + [0.0] * 1534  # orthogonal: cosine = 0

    mcp.create_folder.return_value = {"folderId": "f-workouts-new", "ok": True}
    result = ClassificationResult(topic="Workouts", confidence=0.9, reasoning="gym")
    folder_id = await filer.move_to_topic_folder(
        platform_path=["Sources", "Socials", "Instagram"],
        result=result,
    )

    assert folder_id == "f-workouts-new"
    mcp.create_folder.assert_awaited_once_with("Workouts", parent_folder_id="f-ig")
    embeddings_repo.upsert.assert_awaited_once()


@pytest.mark.asyncio
async def test_move_to_topic_returns_none_when_topic_is_none():
    """Low-confidence classification → topic=None → leave doc at platform root."""
    filer, _, _, _, _ = _make_filer()
    result = ClassificationResult(topic=None, confidence=0.3, reasoning="ambiguous")
    folder_id = await filer.move_to_topic_folder(
        platform_path=["Sources", "Socials", "Instagram"],
        result=result,
    )
    assert folder_id is None
```

- [ ] **Step 5.2: Implement — extend `Filer` in `ingest/src/pipeline/filer.py`**

Add new constructor kwargs + `move_to_topic_folder` method. Don't break existing `Filer(mcp)` callsites — make new args optional with `None` defaults that disable the topic routing.

```python
# Add imports near the top
from src.pipeline.classification import ClassificationResult
from src.pipeline.embeddings import cosine_similarity


class Filer:
    """Stateful folder-resolver bound to an MCPClient. ..."""

    def __init__(
        self,
        mcp: MCPClient,
        *,
        clock: Callable[[], float] = time.monotonic,
        embeddings_repo: Any = None,
        aliases_repo: Any = None,
        embed_fn: Any = None,
        similarity_threshold: float = 0.85,
    ) -> None:
        self._mcp = mcp
        self._clock = clock
        self._tree_snapshot: list[dict] | None = None
        self._tree_fetched_at: float = 0.0
        self._embeddings_repo = embeddings_repo
        self._aliases_repo = aliases_repo
        self._embed_fn = embed_fn
        self._similarity_threshold = similarity_threshold

    # ... existing resolve_or_create_folder + file_doc unchanged ...

    async def move_to_topic_folder(
        self,
        *,
        platform_path: list[str],
        result: ClassificationResult,
    ) -> str | None:
        """Resolve / create the topic subfolder under the given platform.

        Returns the folder_id, or None when result.topic is None
        (confidence-floor case → caller leaves doc at platform_path root).
        """
        if result.topic is None:
            return None
        if self._embeddings_repo is None or self._aliases_repo is None or self._embed_fn is None:
            raise RuntimeError(
                "move_to_topic_folder requires embeddings_repo, aliases_repo, embed_fn"
            )

        platform_folder_id = await self.resolve_or_create_folder(platform_path)
        parent_path = "/".join(platform_path)

        # 1. Pre-recorded alias hit?
        alias_canonical = await self._aliases_repo.lookup(parent_path=parent_path, alias=result.topic)
        if alias_canonical is not None:
            return await self._resolve_existing_topic_folder(platform_folder_id, alias_canonical)

        # 2. Explicit alias_of from classifier?
        if result.alias_of:
            folder_id = await self._resolve_existing_topic_folder(platform_folder_id, result.alias_of)
            await self._aliases_repo.record(
                parent_path=parent_path, alias=result.topic, canonical=result.alias_of,
            )
            return folder_id

        # 3. Embedding-similarity check against existing siblings.
        proposed_vec = await self._embed_fn(result.topic)
        siblings = await self._embeddings_repo.list_for_parent(parent_path)
        best_match: tuple[str, str, float] | None = None  # (folder_id, name, cosine)
        for sib in siblings:
            sim = cosine_similarity(proposed_vec, sib.embedding)
            if sim >= self._similarity_threshold and (best_match is None or sim > best_match[2]):
                best_match = (sib.folder_id, sib.folder_name, sim)
        if best_match is not None:
            await self._aliases_repo.record(
                parent_path=parent_path, alias=result.topic, canonical=best_match[1],
            )
            return best_match[0]

        # 4. Create new folder under platform + persist embedding.
        from src.db import FolderEmbeddingRow

        created = await self._mcp.create_folder(result.topic, parent_folder_id=platform_folder_id)
        new_folder_id = str(created["folderId"])
        await self._embeddings_repo.upsert(FolderEmbeddingRow(
            folder_id=new_folder_id,
            folder_name=result.topic,
            parent_path=parent_path,
            embedding=list(proposed_vec),
        ))
        # Patch in-memory tree so subsequent resolves find the new folder.
        self._patch_tree(platform_path, result.topic, new_folder_id)
        return new_folder_id

    async def _resolve_existing_topic_folder(self, platform_folder_id: str, name: str) -> str:
        """Find a child folder by name under platform_folder_id; raise if missing.

        Used when an alias points us at a name we expect to already exist.
        """
        # Walk the in-memory tree from platform_folder_id.
        await self._ensure_tree()
        target = self._find_node_by_id(platform_folder_id)
        if target is None:
            raise LookupError(f"platform folder {platform_folder_id!r} not in tree snapshot")
        for child in target.get("children", []):
            if child.get("name") == name:
                return str(child["id"])
        raise LookupError(f"alias canonical {name!r} not found under {platform_folder_id!r}")

    def _find_node_by_id(self, folder_id: str) -> dict | None:
        """DFS for a folder node in the cached tree."""
        if self._tree_snapshot is None:
            return None
        stack = list(self._tree_snapshot)
        while stack:
            node = stack.pop()
            if node.get("id") == folder_id:
                return node
            stack.extend(node.get("children", []))
        return None

    def _patch_tree(self, platform_path: list[str], topic: str, new_folder_id: str) -> None:
        """Append a new child node under the platform parent in the cached tree."""
        siblings = self._tree_snapshot or []
        for segment in platform_path:
            match = next((n for n in siblings if n.get("name") == segment), None)
            if match is None:
                return  # tree shape doesn't match; bail without crashing
            siblings = match.setdefault("children", [])
        siblings.append({"id": new_folder_id, "type": "folder", "name": topic, "children": []})
```

- [ ] **Step 5.3: Run + commit**

```bash
cd ingest && python -m pytest tests/test_filer_topic_routing.py -v
git add ingest/src/pipeline/filer.py ingest/tests/test_filer_topic_routing.py
git commit -m "$(cat <<'EOF'
feat(ingest): Filer.move_to_topic_folder — embedding-similarity dedup

Five-step flow per spec §8:
  1. Recorded alias hit (topic_aliases table) → reuse canonical's folder.
  2. Explicit alias_of from classifier → reuse + record alias.
  3. Embedding similarity vs siblings >= threshold → collapse + record.
  4. Otherwise create new folder + embed + persist.
  5. None topic (low confidence) → return None; caller leaves at platform root.

Filer constructor gains optional embeddings_repo / aliases_repo /
embed_fn dependencies. Existing single-arg `Filer(mcp)` callsites
continue to work; calling move_to_topic_folder without injected deps
raises RuntimeError loudly.

Phase 5 / Task 5 of docs/plans/2026-05-07-phase-5-classifier.md
EOF
)"
```

---

## Task 6: Build verification + push + PR

- [ ] **Step 6.1: Full pytest** — expect ~125 passed, ~7 skipped
- [ ] **Step 6.2: `docker compose build ingest`**
- [ ] **Step 6.3: Push branch** — `git push -u origin feat/phase-5-classifier`
- [ ] **Step 6.4: PR via `gh pr create`**

---

## Spec coverage map

| Phase 5 deliverable | Task |
|---|---|
| ClassificationResult model | 1 |
| Embedding helper (OpenAI + cosine) | 2 |
| FolderEmbeddingRepository, TopicAliasRepository | 3 |
| Anthropic Haiku 4.5 classifier with prompt caching | 4 |
| Sibling-aware prompt + topic_hints | 4 |
| Embedding-similarity dedup with topic_aliases | 5 |
| Confidence floor handling (topic=None) | 5 |

## Out of scope

- Worker that calls classifier per capture row → Phase 6
- Replacing the stub doc body with extracted markdown → Phase 6
- Re-classification on existing rows when topic_hints change → not in v1
- Reorganizer scanning Sources/ for new clusters → Phase 8
