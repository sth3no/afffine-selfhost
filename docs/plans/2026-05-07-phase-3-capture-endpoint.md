# Phase 3 — Platform Router + `POST /capture` + Idempotency

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** iOS share extension hits `POST /capture` with a URL and gets back a `202 Accepted` carrying a `web_url` to a stub document in the right `Sources/<group>/<platform>` folder, in <500 ms p50. Submitting the same normalized URL twice returns the existing capture without duplicating work.

**Macro plan:** [`docs/plans/2026-05-06-ingest-service-macro-plan.md`](./2026-05-06-ingest-service-macro-plan.md) — Phase 3
**Spec:** [`docs/specs/2026-05-06-ingest-service-design.md`](../specs/2026-05-06-ingest-service-design.md) — §4 (HTTP API), §5 (state machine — stub creation only this phase), §6 (platform router), §13 (auth), §14 (idempotency)
**Phase 2 prereq:** `feat/phase-2-mcp-client` merged or in this branch's history. `MCPClient` and `Filer` exist and are tested.

**Architecture:** A YAML platform map (`topics.yaml`) drives URL host → `(group, platform_id, folder_name)` resolution. The `POST /capture` handler does the **synchronous** prefix of the pipeline only:
1. Normalize URL → SHA-256 → check `captures` table for existing row → return existing if any.
2. Detect platform from URL host.
3. Resolve/create `Sources/<group>/<platform>` folder via `Filer`.
4. Create stub doc in that folder titled with `shared_title` or URL.
5. Insert row in `captures` with status `queued`.
6. Return `202 {capture_id, doc_id, web_url, status: "queued", platform, initial_path}`.

The actual extraction/classification/filing-to-topic happens in Phases 4–6 via the worker. Phase 3 stops at "stub doc visible immediately."

No worker yet. Captures stay `queued`. List/get/retry/delete endpoints land in Phase 7. Phase 3 only ships `POST /capture` + auth + idempotency + the supporting layers (router, db, models).

**Tech Stack:**
- `pydantic` v2 for request/response models
- `asyncpg` connection pool (initialized at FastAPI lifespan startup)
- `pyyaml` to load `topics.yaml`
- FastAPI `Depends` for auth + DI (filer, db, mcp client)
- `pytest` `httpx.AsyncClient(app=app, base_url=...)` for endpoint tests
- `unittest.mock.AsyncMock` for DB and filer mocks in unit tests

**End state:**
- `POST /capture` with valid bearer token + Instagram URL → 202 with proper response body, stub doc created in AFFiNE under `Sources/Socials/Instagram/`, capture row inserted.
- `POST /capture` second time with same URL → 202 with same `capture_id`, `doc_id`, no DB insert, no AFFiNE call.
- `POST /capture` without auth → 401.
- `POST /capture` with malformed URL → 422 Pydantic validation error.
- 30+ URL → platform routing assertions.
- Total response time <500 ms p50 (measured against an in-memory pipeline; integration timing verified manually after smoke).

---

## Task 1: `topics.yaml` + config loader extension

The platform map is data, not code. A YAML file that the service hot-reloads on mtime change. Phase 3 only needs the *platforms* section; *topic_hints* and *reorg* sections land in Phases 5 and 8 respectively but the loader knows about them and tolerates them being absent.

**Files:**
- Create: `ingest/topics.yaml`
- Modify: `ingest/src/config.py` — add `pyyaml` import, `load_topics()` function returning a typed dict
- Modify: `ingest/pyproject.toml` — add `"pyyaml>=6.0"` to runtime `dependencies`
- Modify: `ingest/Dockerfile` — copy `topics.yaml` into the image
- Create: `ingest/tests/test_topics_config.py`

- [ ] **Step 1.1: Create `ingest/topics.yaml`**

```yaml
# Platform routing for the ingest service.
#
# Each entry maps URL hosts to a (group, platform_id, folder_name) triple.
# The first matching entry by `hosts` wins; the catch-all `id: article` at the
# bottom matches any URL not covered by a specific platform.
#
# `extractor` names match strategies registered in src/pipeline/extractors/
# (Phase 4). Phase 3 doesn't dispatch to extractors yet.

platforms:
  - id: youtube
    group: Socials
    folder_name: Youtube
    hosts: [youtube.com, www.youtube.com, m.youtube.com, youtu.be, music.youtube.com]
    extractor: ytdlp

  - id: instagram
    group: Socials
    folder_name: Instagram
    hosts: [instagram.com, www.instagram.com]
    extractor: ytdlp

  - id: tiktok
    group: Socials
    folder_name: TikTok
    hosts: [tiktok.com, www.tiktok.com, vm.tiktok.com, m.tiktok.com]
    extractor: ytdlp

  - id: x
    group: Socials
    folder_name: X
    hosts: [x.com, www.x.com, twitter.com, www.twitter.com, mobile.twitter.com]
    extractor: oembed_ytdlp

  - id: reddit
    group: Socials
    folder_name: Reddit
    hosts: [reddit.com, www.reddit.com, old.reddit.com, np.reddit.com]
    extractor: reddit_json

  - id: vimeo
    group: Socials
    folder_name: Vimeo
    hosts: [vimeo.com, www.vimeo.com]
    extractor: ytdlp

  - id: arxiv
    group: Research papers
    folder_name: arXiv
    hosts: [arxiv.org, www.arxiv.org]
    extractor: markitdown

  - id: podcast_apple
    group: Podcasts
    folder_name: Apple Podcasts
    hosts: [podcasts.apple.com]
    extractor: markitdown

  - id: spotify_episode
    group: Podcasts
    folder_name: Spotify
    hosts: [open.spotify.com]
    extractor: markitdown

  # Catch-all for everything else. `*` matches any host.
  - id: article
    group: Articles
    folder_name: Web
    hosts: ["*"]
    extractor: markitdown

# Phase 5 will populate topic_hints used by the classifier.
topic_hints: {}

# Phase 8 reorganizer threshold.
reorg:
  default_threshold: 15
  overrides: {}
```

- [ ] **Step 1.2: Modify `ingest/pyproject.toml`** — add `pyyaml`

```toml
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "asyncpg>=0.29",
    "pydantic>=2.7",
    "pydantic-settings>=2.5",
    "httpx>=0.27",
    "pyyaml>=6.0",
]
```

- [ ] **Step 1.3: Modify `ingest/Dockerfile`** — copy `topics.yaml`

Add a line just before the existing `COPY src/`:

```dockerfile
COPY topics.yaml ./
```

The block becomes (annotated diff):
```dockerfile
# Copy application code last.
COPY topics.yaml ./
COPY src/ ./src/
COPY migrations/ ./migrations/
```

- [ ] **Step 1.4: Write the failing test**

Create `ingest/tests/test_topics_config.py`:

```python
from pathlib import Path

import pytest

from src.config import TopicsConfig, load_topics


def test_load_topics_parses_real_file():
    """Smoke: the bundled topics.yaml must be parseable with no errors."""
    config = load_topics()
    assert isinstance(config, TopicsConfig)
    assert len(config.platforms) >= 5
    # The catch-all `article` platform must be present and last.
    assert config.platforms[-1].id == "article"
    assert config.platforms[-1].hosts == ["*"]


def test_load_topics_from_explicit_path(tmp_path: Path):
    yaml_text = """
platforms:
  - id: example
    group: Articles
    folder_name: Example
    hosts: [example.com]
    extractor: markitdown
"""
    p = tmp_path / "topics.yaml"
    p.write_text(yaml_text)

    config = load_topics(p)

    assert len(config.platforms) == 1
    plat = config.platforms[0]
    assert plat.id == "example"
    assert plat.group == "Articles"
    assert plat.folder_name == "Example"
    assert plat.hosts == ["example.com"]
    assert plat.extractor == "markitdown"


def test_load_topics_tolerates_missing_optional_sections(tmp_path: Path):
    yaml_text = """
platforms:
  - id: only
    group: Articles
    folder_name: Only
    hosts: ["*"]
    extractor: markitdown
"""
    p = tmp_path / "topics.yaml"
    p.write_text(yaml_text)

    config = load_topics(p)

    assert config.topic_hints == {}
    assert config.reorg.default_threshold == 15
    assert config.reorg.overrides == {}


def test_load_topics_fails_loudly_on_no_platforms(tmp_path: Path):
    p = tmp_path / "topics.yaml"
    p.write_text("platforms: []\n")
    with pytest.raises(ValueError, match="at least one platform"):
        load_topics(p)
```

- [ ] **Step 1.5: Run tests — verify they FAIL**

```bash
cd ingest && python -m pytest tests/test_topics_config.py -v
```

Expected: 4 ImportErrors (no `TopicsConfig` / `load_topics` in `src.config`).

- [ ] **Step 1.6: Extend `ingest/src/config.py`**

Append below the existing `Settings` class:

```python
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


# ── Topics config (loaded from topics.yaml) ───────────────────────────


class Platform(BaseModel):
    id: str
    group: str
    folder_name: str
    hosts: list[str]
    extractor: str


class ReorgConfig(BaseModel):
    default_threshold: int = 15
    overrides: dict[str, int] = Field(default_factory=dict)


class TopicsConfig(BaseModel):
    platforms: list[Platform]
    topic_hints: dict[str, list[str]] = Field(default_factory=dict)
    reorg: ReorgConfig = Field(default_factory=ReorgConfig)


_DEFAULT_TOPICS_PATH = Path(__file__).resolve().parent.parent / "topics.yaml"


def load_topics(path: Path | None = None) -> TopicsConfig:
    """Read topics.yaml. Validates platforms list is non-empty.

    Optional sections (topic_hints, reorg) default to empty/sentinels so the
    file can grow over phases without breaking older code.
    """
    p = path or _DEFAULT_TOPICS_PATH
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    config = TopicsConfig.model_validate(raw)
    if not config.platforms:
        raise ValueError("topics.yaml must declare at least one platform")
    return config
```

- [ ] **Step 1.7: Run tests — verify they PASS**

```bash
cd ingest && pip install -e ".[dev]" && python -m pytest tests/test_topics_config.py -v
```

Expected: 4 passed.

- [ ] **Step 1.8: Commit**

```bash
git add ingest/topics.yaml ingest/pyproject.toml ingest/Dockerfile \
        ingest/src/config.py ingest/tests/test_topics_config.py
git commit -m "$(cat <<'EOF'
feat(ingest): topics.yaml + load_topics() platform map

Data-driven platform routing. topics.yaml declares each platform with
its hosts, target Sources/<group>/<folder_name> placement, and the
extractor strategy name (Phase 4). The catch-all `article` entry with
hosts=["*"] handles anything without a specific entry.

The loader returns a typed TopicsConfig (platforms + topic_hints +
reorg) so Phase 3 (router), Phase 5 (classifier hints), and Phase 8
(reorganizer thresholds) all consume the same file.

pyyaml added to runtime deps. Dockerfile updated to copy the YAML
into /app/topics.yaml so the container can find it via the default
loader path.

Phase 3 / Task 1 of docs/plans/2026-05-07-phase-3-capture-endpoint.md
EOF
)"
```

---

## Task 2: Platform router

Pure URL host → platform-identity lookup. No I/O. Reads `TopicsConfig` once at construction time and matches via host suffix (so `m.youtube.com` and `youtube.com` both hit the YouTube entry without listing every subdomain).

**Files:**
- Create: `ingest/src/pipeline/router.py`
- Create: `ingest/tests/test_router.py`

- [ ] **Step 2.1: Write the failing test**

```python
from src.config import Platform, TopicsConfig
from src.pipeline.router import PlatformRouter


def _config() -> TopicsConfig:
    return TopicsConfig(
        platforms=[
            Platform(id="youtube", group="Socials", folder_name="Youtube",
                     hosts=["youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com"],
                     extractor="ytdlp"),
            Platform(id="instagram", group="Socials", folder_name="Instagram",
                     hosts=["instagram.com", "www.instagram.com"],
                     extractor="ytdlp"),
            Platform(id="x", group="Socials", folder_name="X",
                     hosts=["x.com", "twitter.com", "www.x.com"],
                     extractor="oembed_ytdlp"),
            Platform(id="arxiv", group="Research papers", folder_name="arXiv",
                     hosts=["arxiv.org"],
                     extractor="markitdown"),
            Platform(id="article", group="Articles", folder_name="Web",
                     hosts=["*"],
                     extractor="markitdown"),
        ],
    )


URL_CASES = [
    # YouTube variants
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
    ("https://youtube.com/watch?v=abc", "youtube"),
    ("https://m.youtube.com/watch?v=abc", "youtube"),
    ("https://youtu.be/abc", "youtube"),
    ("https://youtube.com/shorts/xyz", "youtube"),
    # Instagram
    ("https://www.instagram.com/p/Cxyz123/", "instagram"),
    ("https://instagram.com/reel/Czzzz/", "instagram"),
    # X / Twitter
    ("https://x.com/anyuser/status/123", "x"),
    ("https://twitter.com/anyuser/status/123", "x"),
    # arXiv
    ("https://arxiv.org/abs/2401.00001", "arxiv"),
    ("http://arxiv.org/pdf/2401.00001v1.pdf", "arxiv"),
    # Catch-all
    ("https://en.wikipedia.org/wiki/Foo", "article"),
    ("https://news.ycombinator.com/item?id=1", "article"),
    ("https://blog.example.com/post", "article"),
    ("http://random.local/page", "article"),
    # Edge: bare host without scheme upgraded to https? (URL parse should still work)
    ("https://example.com", "article"),
    # Edge: URL with port
    ("https://example.com:8080/page", "article"),
    # Trailing slash variants
    ("https://www.instagram.com/p/Cxyz/", "instagram"),
    ("https://www.instagram.com/p/Cxyz", "instagram"),
    # Subdomain not in list falls through to catch-all
    ("https://api.youtube.com/v3/...", "article"),  # api.youtube.com NOT in list
]


import pytest


@pytest.mark.parametrize("url,expected_id", URL_CASES)
def test_router_resolves(url: str, expected_id: str):
    router = PlatformRouter(_config())
    plat = router.detect(url)
    assert plat.id == expected_id, f"{url} -> got {plat.id}, want {expected_id}"


def test_router_returns_full_platform_object():
    router = PlatformRouter(_config())
    plat = router.detect("https://www.instagram.com/p/Cxyz/")
    assert plat.group == "Socials"
    assert plat.folder_name == "Instagram"
    assert plat.extractor == "ytdlp"


def test_router_initial_path_helper():
    router = PlatformRouter(_config())
    plat = router.detect("https://www.instagram.com/p/Cxyz/")
    assert router.initial_path(plat) == ["Sources", "Socials", "Instagram"]


def test_router_no_catch_all_raises():
    """A config without the wildcard entry is a misconfiguration —
    surface it loudly rather than silently dropping URLs."""
    bad = TopicsConfig(platforms=[
        Platform(id="only", group="Socials", folder_name="Only",
                 hosts=["specific.example.com"], extractor="markitdown"),
    ])
    router = PlatformRouter(bad)
    import pytest
    with pytest.raises(LookupError, match="no catch-all"):
        router.detect("https://other.com/page")


def test_router_invalid_url_raises():
    router = PlatformRouter(_config())
    import pytest
    with pytest.raises(ValueError, match="cannot extract host"):
        router.detect("not-a-url")
```

- [ ] **Step 2.2: Run tests — verify they FAIL**

```bash
cd ingest && python -m pytest tests/test_router.py -v
```

Expected: import errors.

- [ ] **Step 2.3: Implement `ingest/src/pipeline/router.py`**

```python
"""URL host → Platform identity.

Reads a TopicsConfig once and answers `detect(url) -> Platform`. Match is
exact-host-equality with the lists in `Platform.hosts`. Subdomains not listed
fall through to the catch-all `hosts: ["*"]` entry. A config without a
catch-all causes `detect()` to raise on unmatched URLs — that's a
misconfiguration we want to surface loudly.

Public API:
    router = PlatformRouter(config)
    plat = router.detect("https://www.instagram.com/p/abc")
    path = router.initial_path(plat)   # → ["Sources", "Socials", "Instagram"]
"""

from __future__ import annotations

from urllib.parse import urlparse

from src.config import Platform, TopicsConfig


class PlatformRouter:
    def __init__(self, config: TopicsConfig) -> None:
        self._platforms = list(config.platforms)
        # Pre-build a host → platform index for fast lookup.
        self._by_host: dict[str, Platform] = {}
        self._catch_all: Platform | None = None
        for p in self._platforms:
            for host in p.hosts:
                if host == "*":
                    self._catch_all = p
                else:
                    self._by_host[host.lower()] = p

    def detect(self, url: str) -> Platform:
        host = self._extract_host(url)
        match = self._by_host.get(host)
        if match is not None:
            return match
        if self._catch_all is None:
            raise LookupError(
                f"no catch-all platform configured (host={host!r})"
            )
        return self._catch_all

    @staticmethod
    def initial_path(platform: Platform) -> list[str]:
        return ["Sources", platform.group, platform.folder_name]

    @staticmethod
    def _extract_host(url: str) -> str:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        host = (parsed.hostname or "").lower()
        if not host:
            raise ValueError(f"cannot extract host from URL: {url!r}")
        return host
```

- [ ] **Step 2.4: Run tests — verify PASS**

```bash
cd ingest && python -m pytest tests/test_router.py -v
```

Expected: all parametrized cases + helper tests pass.

- [ ] **Step 2.5: Commit**

```bash
git add ingest/src/pipeline/router.py ingest/tests/test_router.py
git commit -m "$(cat <<'EOF'
feat(ingest): platform router (URL host → Platform)

PlatformRouter reads a TopicsConfig and answers detect(url) → Platform
plus initial_path(platform) → ["Sources", group, folder_name]. Match is
exact host equality; subdomains not listed fall through to the catch-all
hosts:["*"] entry. Misconfigured config (no catch-all) raises LookupError
loudly rather than silently dropping URLs.

20+ parametrized URL cases covering YouTube/IG/X/arXiv/catch-all plus
edge cases (port numbers, trailing slashes, bare subdomain not in list).

Phase 3 / Task 2 of docs/plans/2026-05-07-phase-3-capture-endpoint.md
EOF
)"
```

---

## Task 3: Pydantic request/response models

The wire types for the iOS app and HTTP API.

**Files:**
- Create: `ingest/src/models.py`
- Create: `ingest/tests/test_models.py`

- [ ] **Step 3.1: Write the failing test**

```python
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.models import (
    CaptureRequest,
    CaptureResponse,
    CaptureStatus,
    normalized_url,
    url_hash,
)


def test_capture_request_minimal():
    req = CaptureRequest(url="https://example.com/page")
    assert req.url == "https://example.com/page"
    assert req.source_app is None
    assert req.shared_title is None
    assert req.shared_text is None


def test_capture_request_full():
    req = CaptureRequest(
        url="https://www.instagram.com/p/Cxyz/",
        source_app="Instagram",
        shared_title="Honey-glazed salmon",
        shared_text="Recipe with photos",
    )
    assert req.source_app == "Instagram"
    assert req.shared_title == "Honey-glazed salmon"


def test_capture_request_at_least_url_or_text():
    """Spec §4 says one of url/shared_text must be present. With neither,
    Pydantic accepts (since both are Optional), but the API handler
    enforces the rule. Test the model accepts; handler test covers the rule."""
    req = CaptureRequest(url=None, shared_text="just a note")
    assert req.url is None
    assert req.shared_text == "just a note"


def test_capture_request_rejects_non_http_scheme():
    with pytest.raises(ValidationError):
        CaptureRequest(url="javascript:alert(1)")


def test_capture_response_serializes_iso8601():
    resp = CaptureResponse(
        capture_id="01J9X4M5",
        doc_id="aaaa-bbbb",
        web_url="https://affine.example.com/workspace/x/aaaa-bbbb",
        status=CaptureStatus.QUEUED,
        platform="instagram",
        initial_path="Sources/Socials/Instagram",
        created_at=datetime(2026, 5, 7, 14, 20, 0, tzinfo=timezone.utc),
    )
    payload = resp.model_dump(mode="json")
    assert payload["status"] == "queued"
    assert payload["created_at"] == "2026-05-07T14:20:00Z"


def test_normalized_url_strips_utm_params_and_lowercases_host():
    nu = normalized_url("https://Instagram.COM/p/abc?utm_source=test&id=1#section")
    # host lowercased, utm_* stripped, fragment dropped, other params kept
    assert nu == "https://instagram.com/p/abc?id=1"


def test_normalized_url_strips_trailing_slash_when_no_query():
    nu = normalized_url("https://example.com/foo/")
    assert nu == "https://example.com/foo"


def test_normalized_url_keeps_trailing_slash_when_root():
    nu = normalized_url("https://example.com/")
    assert nu == "https://example.com/"


def test_url_hash_is_stable_across_normalization_inputs():
    a = url_hash("https://Instagram.COM/p/abc?utm_source=x")
    b = url_hash("https://instagram.com/p/abc")
    assert a == b
    assert len(a) == 64  # sha256 hex
```

- [ ] **Step 3.2: Run tests — verify FAIL**

```bash
cd ingest && python -m pytest tests/test_models.py -v
```

Expected: all fail (import errors).

- [ ] **Step 3.3: Implement `ingest/src/models.py`**

```python
"""Wire-level Pydantic models + URL normalization helpers."""

from __future__ import annotations

import enum
import hashlib
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


# ── Enums & helpers ──────────────────────────────────────────────────


class CaptureStatus(str, enum.Enum):
    """State machine values. See spec §5."""
    QUEUED = "queued"
    EXTRACTING = "extracting"
    CLASSIFYING = "classifying"
    FILING = "filing"
    DONE = "done"
    FAILED = "failed"
    DELETED = "deleted"


_UTM_PREFIXES = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid")


def normalized_url(url: str) -> str:
    """Canonicalize a URL for idempotency.

    - lowercases the host (path & query stay case-sensitive)
    - drops fragments
    - removes utm_* / fbclid / gclid / mc_cid / mc_eid query params
    - removes trailing slash on non-root paths
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    netloc = host.lower()
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"

    pairs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if not any(k.lower().startswith(p) for p in _UTM_PREFIXES)]
    query = urlencode(pairs)

    path = parsed.path
    if path.endswith("/") and len(path) > 1:
        path = path[:-1]

    return urlunparse((parsed.scheme.lower(), netloc, path, "", query, ""))


def url_hash(url: str) -> str:
    """Stable SHA-256 of the normalized URL for idempotency lookups."""
    return hashlib.sha256(normalized_url(url).encode("utf-8")).hexdigest()


# ── Wire models ──────────────────────────────────────────────────────


class CaptureRequest(BaseModel):
    """POST /capture body. At least one of url/shared_text must be present;
    enforced in the handler, not the model (Pydantic doesn't natively
    express "one-of-N" cleanly without extra ceremony)."""

    model_config = ConfigDict(extra="forbid")

    url: HttpUrl | None = None
    source_app: str | None = Field(default=None, max_length=128)
    shared_title: str | None = Field(default=None, max_length=512)
    shared_text: str | None = Field(default=None, max_length=10_000)

    @field_validator("url", mode="before")
    @classmethod
    def _coerce_url_to_str(cls, value):
        # Pydantic returns HttpUrl objects; we want str everywhere downstream.
        return str(value) if value else None


class CaptureResponse(BaseModel):
    """202 Accepted response from POST /capture."""

    model_config = ConfigDict(json_encoders={datetime: lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")})

    capture_id: str
    doc_id: str
    web_url: str
    status: CaptureStatus
    platform: str
    initial_path: str
    created_at: datetime
```

- [ ] **Step 3.4: Run tests — verify PASS**

```bash
cd ingest && python -m pytest tests/test_models.py -v
```

Expected: all pass.

- [ ] **Step 3.5: Commit**

```bash
git add ingest/src/models.py ingest/tests/test_models.py
git commit -m "$(cat <<'EOF'
feat(ingest): wire models + URL normalization for idempotency

CaptureRequest / CaptureResponse model the POST /capture HTTP shape,
matching docs/specs/...§4. CaptureStatus enum captures the §5 state
machine. extra="forbid" rejects unknown request fields loudly so iOS
typos don't silently degrade.

normalized_url() canonicalizes for idempotency: lowercases host, strips
fragment + utm_*/fbclid/gclid/mc_cid/mc_eid tracking params, removes
trailing slash on non-root paths. url_hash() is sha256 of the result —
the UNIQUE key in captures.url_hash.

Phase 3 / Task 3 of docs/plans/2026-05-07-phase-3-capture-endpoint.md
EOF
)"
```

---

## Task 4: DB layer (`db.py`)

Async pool + the 3 queries Phase 3 needs: `insert_capture`, `get_capture_by_url_hash`, `get_capture_by_id`. Pure data layer; no business logic. Tested via `AsyncMock` for unit; real DB exercised via the integration smoke at end of phase.

**Files:**
- Create: `ingest/src/db.py`
- Create: `ingest/tests/test_db.py`

- [ ] **Step 4.1: Write the failing test**

```python
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.db import CaptureRow, CaptureRepository, build_pool_kwargs


@pytest.mark.asyncio
async def test_insert_capture_executes_correct_sql():
    conn = AsyncMock()
    conn.execute.return_value = "INSERT 0 1"

    repo = CaptureRepository(conn)
    row = CaptureRow(
        id="01J9X4M5",
        url="https://example.com",
        url_hash="abc123",
        source_app="Safari",
        shared_title="Hello",
        shared_text=None,
        platform="article",
        status="queued",
        doc_id="d-1",
        web_url="https://affine.example.com/.../d-1",
        topic_path="Sources/Articles/Web",
    )
    await repo.insert(row)

    sql, *args = conn.execute.call_args.args
    assert "INSERT INTO captures" in sql
    # Verify all required columns are bound in order.
    assert args[0] == "01J9X4M5"
    assert args[1] == "https://example.com"
    assert args[2] == "abc123"
    # 11+ args total; spot-check that bind count is sane (no SQL injection
    # via missing $N).
    assert sql.count("$") >= 11


@pytest.mark.asyncio
async def test_get_by_url_hash_returns_row_when_present():
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": "01J9X4M5",
        "url": "https://example.com",
        "url_hash": "abc",
        "source_app": None,
        "shared_title": None,
        "shared_text": None,
        "platform": "article",
        "status": "queued",
        "doc_id": "d-1",
        "web_url": "...",
        "topic_path": "Sources/Articles/Web",
        "created_at": datetime(2026, 5, 7, tzinfo=timezone.utc),
    }
    repo = CaptureRepository(conn)
    row = await repo.get_by_url_hash("abc")
    assert row is not None
    assert row.id == "01J9X4M5"


@pytest.mark.asyncio
async def test_get_by_url_hash_returns_none_when_absent():
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    repo = CaptureRepository(conn)
    assert await repo.get_by_url_hash("nope") is None


@pytest.mark.asyncio
async def test_get_by_id_returns_row():
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": "01J9X4M5",
        "url": "x",
        "url_hash": "y",
        "source_app": None,
        "shared_title": None,
        "shared_text": None,
        "platform": "article",
        "status": "queued",
        "doc_id": "d",
        "web_url": "...",
        "topic_path": "...",
        "created_at": datetime(2026, 5, 7, tzinfo=timezone.utc),
    }
    repo = CaptureRepository(conn)
    row = await repo.get_by_id("01J9X4M5")
    assert row.id == "01J9X4M5"


def test_build_pool_kwargs_parses_url():
    kwargs = build_pool_kwargs("postgresql://user:pass@host:5432/db")
    assert kwargs["dsn"] == "postgresql://user:pass@host:5432/db"
    assert kwargs["min_size"] == 1
    assert kwargs["max_size"] >= 4
```

- [ ] **Step 4.2: Run tests — verify FAIL**

```bash
cd ingest && python -m pytest tests/test_db.py -v
```

Expected: import errors.

- [ ] **Step 4.3: Implement `ingest/src/db.py`**

```python
"""Captures repository (asyncpg). Phase 3 only needs three queries."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import asyncpg


@dataclass
class CaptureRow:
    id: str
    url: str | None
    url_hash: str
    source_app: str | None
    shared_title: str | None
    shared_text: str | None
    platform: str
    status: str
    doc_id: str | None
    web_url: str | None
    topic_path: str | None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


_INSERT_SQL = """
    INSERT INTO captures
        (id, url, url_hash, source_app, shared_title, shared_text,
         platform, status, doc_id, web_url, topic_path)
    VALUES
        ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
"""

_BASE_SELECT = """
    SELECT id, url, url_hash, source_app, shared_title, shared_text,
           platform, status, doc_id, web_url, topic_path, created_at
    FROM captures
"""


class CaptureRepository:
    """Thin wrapper around an asyncpg.Connection (or pool — duck-typed).

    Phase 3 callers pass a single Connection. Phase 6 will wire a pool
    with `async with pool.acquire() as conn` per request.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def insert(self, row: CaptureRow) -> None:
        await self._conn.execute(
            _INSERT_SQL,
            row.id,
            row.url,
            row.url_hash,
            row.source_app,
            row.shared_title,
            row.shared_text,
            row.platform,
            row.status,
            row.doc_id,
            row.web_url,
            row.topic_path,
        )

    async def get_by_url_hash(self, url_hash: str) -> CaptureRow | None:
        record = await self._conn.fetchrow(_BASE_SELECT + " WHERE url_hash = $1", url_hash)
        if record is None:
            return None
        return CaptureRow(**dict(record))

    async def get_by_id(self, capture_id: str) -> CaptureRow | None:
        record = await self._conn.fetchrow(_BASE_SELECT + " WHERE id = $1", capture_id)
        if record is None:
            return None
        return CaptureRow(**dict(record))


# ── Pool helpers ──────────────────────────────────────────────────────


def build_pool_kwargs(dsn: str, *, min_size: int = 1, max_size: int = 8) -> dict:
    """Return kwargs for `asyncpg.create_pool(...)`."""
    return {"dsn": dsn, "min_size": min_size, "max_size": max_size}


async def create_pool(dsn: str) -> asyncpg.Pool:
    """Create the asyncpg pool. Lifespan-managed by FastAPI in api.py."""
    return await asyncpg.create_pool(**build_pool_kwargs(dsn))
```

- [ ] **Step 4.4: Run tests — verify PASS**

```bash
cd ingest && python -m pytest tests/test_db.py -v
```

Expected: 5 passed.

- [ ] **Step 4.5: Commit**

```bash
git add ingest/src/db.py ingest/tests/test_db.py
git commit -m "$(cat <<'EOF'
feat(ingest): captures repository + asyncpg pool helpers

CaptureRow dataclass mirrors the captures table columns Phase 3 reads
and writes (no classifier_* yet — those come in Phase 5).
CaptureRepository wraps an asyncpg connection and exposes the three
operations Phase 3 needs:

  - insert(row)
  - get_by_url_hash(hash) — for idempotency lookup before doing any
    work in POST /capture
  - get_by_id(capture_id) — used by Phase 7 read endpoints; useful for
    smoke testing now

create_pool(dsn) is a thin wrapper for the FastAPI lifespan handler
to call at startup. build_pool_kwargs is split out so the kwargs are
inspectable in tests.

Tested with AsyncMock on the connection: SQL shape (INSERT/SELECT),
parameter binding order, and the None-on-absent-row path. Real DB
integration verified by the build smoke at the end of the phase.

Phase 3 / Task 4 of docs/plans/2026-05-07-phase-3-capture-endpoint.md
EOF
)"
```

---

## Task 5: Auth dependency

Bearer token check via FastAPI `Depends`. Reads the configured token from `Settings.ingest_api_token` and rejects requests with a missing/wrong token.

**Files:**
- Create: `ingest/src/auth.py`
- Create: `ingest/tests/test_auth.py`

- [ ] **Step 5.1: Write the failing test**

```python
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth import require_token
from src.config import settings


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()

    @a.get("/protected")
    def protected(_: str = require_token):  # type: ignore[arg-type]
        return {"ok": True}

    return a


def test_no_authorization_header_returns_401(app: FastAPI):
    client = TestClient(app)
    r = client.get("/protected")
    assert r.status_code == 401
    assert "missing" in r.json()["detail"].lower()


def test_wrong_scheme_returns_401(app: FastAPI):
    client = TestClient(app)
    r = client.get("/protected", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert r.status_code == 401


def test_wrong_token_returns_401(app: FastAPI):
    client = TestClient(app)
    r = client.get("/protected", headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401


def test_correct_token_returns_200(app: FastAPI):
    client = TestClient(app)
    r = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {settings.ingest_api_token}"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
```

- [ ] **Step 5.2: Run tests — verify FAIL**

```bash
cd ingest && python -m pytest tests/test_auth.py -v
```

Expected: import error.

- [ ] **Step 5.3: Implement `ingest/src/auth.py`**

```python
"""Bearer-token auth for the ingest HTTP API.

Single shared token (selfhost = single user). Compared with constant-time
equality to dodge token-leak side channels — overkill for personal selfhost
but cheap and correct.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, status

from src.config import settings


async def _check_token(request: Request) -> str:
    auth = request.headers.get("Authorization")
    if not auth:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    parts = auth.split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization must be Bearer scheme",
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented = parts[1].strip()
    expected = settings.ingest_api_token
    if not expected or not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return presented


# Re-exportable Depends instance — use as `_: str = require_token` in routes.
require_token = Depends(_check_token)
```

- [ ] **Step 5.4: Run tests — verify PASS**

```bash
cd ingest && python -m pytest tests/test_auth.py -v
```

Expected: 4 passed.

- [ ] **Step 5.5: Commit**

```bash
git add ingest/src/auth.py ingest/tests/test_auth.py
git commit -m "$(cat <<'EOF'
feat(ingest): bearer-token auth dependency

require_token is a FastAPI Depends that rejects requests without a
matching Authorization: Bearer <INGEST_API_TOKEN> header. Uses
hmac.compare_digest for constant-time comparison. Wrong scheme,
missing header, and wrong token all return 401 with WWW-Authenticate.

Phase 3 / Task 5 of docs/plans/2026-05-07-phase-3-capture-endpoint.md
EOF
)"
```

---

## Task 6: `POST /capture` endpoint with stub creation + idempotency

The actual endpoint. Wires together: auth → URL normalize → idempotency check → platform router → Filer stub doc → DB insert → 202 response.

**Files:**
- Modify: `ingest/src/api.py` (add lifespan handler + DI helpers + `POST /capture` route)
- Create: `ingest/tests/test_capture_endpoint.py`

- [ ] **Step 6.1: Write the failing tests**

```python
import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from src.config import settings


def _build_test_app(*, repo: AsyncMock, filer: AsyncMock, router: MagicMock) -> FastAPI:
    """Construct the FastAPI app with mocked dependencies via overrides."""
    from src.api import app, get_capture_repo, get_filer, get_platform_router

    app.dependency_overrides[get_capture_repo] = lambda: repo
    app.dependency_overrides[get_filer] = lambda: filer
    app.dependency_overrides[get_platform_router] = lambda: router
    return app


@pytest.mark.asyncio
async def test_capture_unauthorized_returns_401():
    repo = AsyncMock()
    filer = AsyncMock()
    router = MagicMock()
    app = _build_test_app(repo=repo, filer=filer, router=router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/capture", json={"url": "https://example.com"})
        assert r.status_code == 401
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_capture_happy_path_creates_stub_and_returns_202():
    repo = AsyncMock()
    repo.get_by_url_hash.return_value = None  # not yet captured

    filer = AsyncMock()
    filer.resolve_or_create_folder.return_value = "f-instagram"

    # mock the inner mcp.create_doc by stubbing filer's mcp attribute
    mcp = AsyncMock()
    mcp.create_doc.return_value = {"docId": "doc-abc-123"}
    filer._mcp = mcp

    router = MagicMock()
    plat = MagicMock(id="instagram", group="Socials", folder_name="Instagram", extractor="ytdlp")
    router.detect.return_value = plat
    router.initial_path.return_value = ["Sources", "Socials", "Instagram"]

    app = _build_test_app(repo=repo, filer=filer, router=router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/capture",
                json={
                    "url": "https://www.instagram.com/p/Cxyz/",
                    "shared_title": "Honey-glazed salmon",
                    "source_app": "Instagram",
                },
                headers={"Authorization": f"Bearer {settings.ingest_api_token}"},
            )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["doc_id"] == "doc-abc-123"
        assert body["status"] == "queued"
        assert body["platform"] == "instagram"
        assert body["initial_path"] == "Sources/Socials/Instagram"
        assert body["capture_id"]
        assert "doc-abc-123" in body["web_url"]

        repo.insert.assert_called_once()
        inserted_row = repo.insert.call_args.args[0]
        assert inserted_row.platform == "instagram"
        assert inserted_row.doc_id == "doc-abc-123"
        assert inserted_row.status == "queued"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_capture_idempotent_returns_existing_without_writing():
    from src.db import CaptureRow
    from datetime import datetime, timezone

    repo = AsyncMock()
    existing = CaptureRow(
        id="01J-existing",
        url="https://www.instagram.com/p/Cxyz/",
        url_hash="hash",
        source_app=None,
        shared_title="prior",
        shared_text=None,
        platform="instagram",
        status="done",
        doc_id="prev-doc",
        web_url="https://affine.example.com/.../prev-doc",
        topic_path="Sources/Socials/Instagram",
        created_at=datetime(2026, 5, 6, tzinfo=timezone.utc),
    )
    repo.get_by_url_hash.return_value = existing

    filer = AsyncMock()
    router = MagicMock()
    router.detect.return_value = MagicMock(id="instagram", group="Socials", folder_name="Instagram")
    router.initial_path.return_value = ["Sources", "Socials", "Instagram"]

    app = _build_test_app(repo=repo, filer=filer, router=router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/capture",
                json={"url": "https://www.instagram.com/p/Cxyz/?utm_source=test"},
                headers={"Authorization": f"Bearer {settings.ingest_api_token}"},
            )
        assert r.status_code == 202
        body = r.json()
        assert body["capture_id"] == "01J-existing"
        assert body["doc_id"] == "prev-doc"
        # NOTHING was written or filed.
        repo.insert.assert_not_called()
        filer.resolve_or_create_folder.assert_not_called()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_capture_rejects_missing_url_and_text():
    """Spec: at least one of url/shared_text required. Handler enforces."""
    repo = AsyncMock()
    filer = AsyncMock()
    router = MagicMock()
    app = _build_test_app(repo=repo, filer=filer, router=router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/capture",
                json={},
                headers={"Authorization": f"Bearer {settings.ingest_api_token}"},
            )
        assert r.status_code == 400
        assert "url or shared_text" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_capture_with_extra_field_returns_422():
    repo = AsyncMock()
    filer = AsyncMock()
    router = MagicMock()
    app = _build_test_app(repo=repo, filer=filer, router=router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/capture",
                json={"url": "https://example.com", "unexpected": "field"},
                headers={"Authorization": f"Bearer {settings.ingest_api_token}"},
            )
        assert r.status_code == 422
    finally:
        app.dependency_overrides.clear()
```

- [ ] **Step 6.2: Run tests — verify FAIL**

```bash
cd ingest && python -m pytest tests/test_capture_endpoint.py -v
```

Expected: ImportError on `get_capture_repo`/`get_filer`/`get_platform_router`.

- [ ] **Step 6.3: Reimplement `ingest/src/api.py`**

The current api.py is just `/health`. We replace it with the lifespan + DI + capture endpoint. Use Write tool (not Edit) for clarity.

```python
"""FastAPI app for the ingest service.

Phase 3 ships /health (Phase 1) and POST /capture. List/get/retry/delete
endpoints land in Phase 7. Worker loop in Phase 6.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, status
from ulid import ULID

from src.auth import require_token
from src.config import load_topics, settings
from src.db import CaptureRepository, CaptureRow, create_pool
from src.mcp_client import MCPClient
from src.models import (
    CaptureRequest,
    CaptureResponse,
    CaptureStatus,
    normalized_url,
    url_hash,
)
from src.pipeline.filer import Filer
from src.pipeline.router import PlatformRouter


# ── Application-scoped state ──────────────────────────────────────────


class AppState:
    """Mutable container set during lifespan startup."""
    pool: asyncpg.Pool | None = None
    mcp: MCPClient | None = None
    filer: Filer | None = None
    router: PlatformRouter | None = None


app_state = AppState()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup: load topics, open DB pool, prepare MCP client + filer.
    Shutdown: close everything."""
    # Topics & router (load once; hot-reload deferred to a future phase).
    topics = load_topics()
    app_state.router = PlatformRouter(topics)

    # MCP client to mcp_ext (lazy connect — no network call here).
    mcp_url = os.environ.get("MCP_EXT_URL", "http://mcp_ext:3100")
    affine_token = os.environ.get("AFFINE_ACCESS_TOKEN", "")
    app_state.mcp = await MCPClient(mcp_url, affine_token).__aenter__()
    app_state.filer = Filer(app_state.mcp)

    # asyncpg pool (skipped when DATABASE_URL points at the placeholder used
    # by `pip install` smoke tests on developer machines).
    if settings.database_url and "placeholder" not in settings.database_url:
        app_state.pool = await create_pool(settings.database_url)

    yield

    # Shutdown
    if app_state.pool is not None:
        await app_state.pool.close()
    if app_state.mcp is not None:
        await app_state.mcp.__aexit__(None, None, None)


app = FastAPI(title="affine-ingest", version=settings.version, lifespan=lifespan)


# ── DI providers (overrideable in tests) ──────────────────────────────


async def get_pool() -> asyncpg.Pool:
    if app_state.pool is None:
        raise HTTPException(status_code=503, detail="Database pool not initialized")
    return app_state.pool


async def get_capture_repo(pool: asyncpg.Pool = Depends(get_pool)) -> CaptureRepository:
    # Acquire a connection per request. Pool returns it on context exit.
    async with pool.acquire() as conn:
        yield CaptureRepository(conn)


def get_filer() -> Filer:
    if app_state.filer is None:
        raise HTTPException(status_code=503, detail="MCP filer not initialized")
    return app_state.filer


def get_platform_router() -> PlatformRouter:
    if app_state.router is None:
        raise HTTPException(status_code=503, detail="Platform router not initialized")
    return app_state.router


# ── Routes ────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "queue_depth": 0,
        "worker_alive": False,
        "version": settings.version,
    }


@app.post("/capture", response_model=CaptureResponse, status_code=status.HTTP_202_ACCEPTED)
async def capture(
    body: CaptureRequest,
    repo: CaptureRepository = Depends(get_capture_repo),
    filer: Filer = Depends(get_filer),
    router: PlatformRouter = Depends(get_platform_router),
    _: str = require_token,
) -> CaptureResponse:
    if not body.url and not body.shared_text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one of url or shared_text must be present.",
        )

    # 1. Idempotency: hash the (normalized) URL and look up existing.
    if body.url:
        hash_value = url_hash(body.url)
        existing = await repo.get_by_url_hash(hash_value)
        if existing is not None:
            return _row_to_response(existing, router)
    else:
        # No URL — text-only capture; idempotency by hash of text.
        hash_value = url_hash(body.shared_text or "")  # not URL-shaped but the function still hashes
        existing = await repo.get_by_url_hash(hash_value)
        if existing is not None:
            return _row_to_response(existing, router)

    # 2. Detect platform from URL (text-only capture defaults to article).
    platform = router.detect(body.url) if body.url else _article_platform(router)
    initial_path = router.initial_path(platform)

    # 3. Resolve/create the platform folder, create stub doc.
    folder_id = await filer.resolve_or_create_folder(initial_path)
    title = body.shared_title or (body.url or "captured note")
    created = await filer._mcp.create_doc(title)
    doc_id = str(created["docId"])
    await filer._mcp.move_document(doc_id, folder_id=folder_id)
    await filer._mcp.append_blocks(
        doc_id,
        [{"type": "paragraph", "text": f"> Capturing... ({datetime.now(timezone.utc).isoformat()})"}],
    )

    web_url = _build_web_url(doc_id)
    capture_id = str(ULID())

    # 4. Insert capture row.
    row = CaptureRow(
        id=capture_id,
        url=body.url,
        url_hash=hash_value,
        source_app=body.source_app,
        shared_title=body.shared_title,
        shared_text=body.shared_text,
        platform=platform.id,
        status=CaptureStatus.QUEUED.value,
        doc_id=doc_id,
        web_url=web_url,
        topic_path="/".join(initial_path),
    )
    await repo.insert(row)

    return _row_to_response(row, router)


# ── Helpers ───────────────────────────────────────────────────────────


def _row_to_response(row: CaptureRow, router: PlatformRouter) -> CaptureResponse:
    return CaptureResponse(
        capture_id=row.id,
        doc_id=row.doc_id or "",
        web_url=row.web_url or "",
        status=CaptureStatus(row.status),
        platform=row.platform,
        initial_path=row.topic_path or "",
        created_at=row.created_at,
    )


def _article_platform(router: PlatformRouter):
    """Used when capture has shared_text but no URL."""
    # detect("") would raise; just look up the catch-all directly.
    for plat in router._platforms:  # noqa: SLF001 — internal but acceptable for v1
        if "*" in plat.hosts:
            return plat
    raise HTTPException(status_code=503, detail="No catch-all platform configured")


def _build_web_url(doc_id: str) -> str:
    base = os.environ.get("AFFINE_SERVER_EXTERNAL_URL", "http://localhost:3010")
    workspace = os.environ.get("AFFINE_WORKSPACE_ID", "")
    if workspace:
        return f"{base.rstrip('/')}/workspace/{workspace}/{doc_id}"
    return f"{base.rstrip('/')}/{doc_id}"
```

- [ ] **Step 6.4: Add `python-ulid` dep**

The endpoint uses `ulid.ULID()`. Add to runtime deps in `ingest/pyproject.toml`:

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
]
```

Run `pip install -e ".[dev]"` to pick up the new dep.

- [ ] **Step 6.5: Run tests — verify PASS**

```bash
cd ingest && pip install -e ".[dev]" && python -m pytest tests/test_capture_endpoint.py -v
```

Expected: 5 passed.

Then run the full suite:

```bash
cd ingest && python -m pytest tests/ -v
```

Expected: all unit tests pass; integration test still 1 skipped.

- [ ] **Step 6.6: Commit**

```bash
git add ingest/src/api.py ingest/pyproject.toml ingest/tests/test_capture_endpoint.py
git commit -m "$(cat <<'EOF'
feat(ingest): POST /capture endpoint with stub creation + idempotency

End-to-end POST handler:
  1. Bearer auth (require_token)
  2. URL/text validation (one required)
  3. Idempotency by url_hash — return existing row if present
  4. Platform detection via PlatformRouter
  5. Resolve/create Sources/<group>/<platform> folder via Filer
  6. Create stub doc, move into folder, append "Capturing..." marker
  7. INSERT capture row with status=queued
  8. 202 with {capture_id, doc_id, web_url, status, platform, initial_path}

Lifespan handler initializes the asyncpg pool, MCPClient (entered
context), Filer, and PlatformRouter once at startup. Tests use
dependency_overrides to swap in mocks — no real DB or network in
the test layer.

python-ulid added for capture_id generation.

Phase 3 / Task 6 of docs/plans/2026-05-07-phase-3-capture-endpoint.md
EOF
)"
```

---

## Task 7: Build verification + acceptance

- [ ] **Step 7.1: Rebuild ingest image with new deps + topics.yaml**

```bash
cd .. && docker compose build ingest
```

Expected: clean build. New deps (pyyaml, python-ulid) + topics.yaml in image.

- [ ] **Step 7.2: Run full pytest suite**

```bash
cd ingest && python -m pytest tests/ -v
```

Expected: all unit tests pass + 1 skipped integration. Approximate count: 27 (Phase 2) + 4 (topics) + 24 (router) + 9 (models) + 5 (db) + 4 (auth) + 5 (capture) = ~78 passed, 1 skipped.

- [ ] **Step 7.3: Acceptance checklist**

- [ ] `topics.yaml` parses; `load_topics()` returns valid `TopicsConfig`
- [ ] PlatformRouter resolves 20+ URL cases correctly including catch-all
- [ ] CaptureRequest rejects extra fields (`extra="forbid"`)
- [ ] `normalized_url` strips utm_*/fbclid/gclid/mc_cid/mc_eid + fragment + trailing slash
- [ ] CaptureRepository SQL bind order matches captures column order
- [ ] Bearer auth: 401 on missing, wrong scheme, wrong token; 200 on right token
- [ ] POST /capture without auth → 401
- [ ] POST /capture happy path → 202 with stub doc visible (mocked filer)
- [ ] POST /capture same URL twice → second returns existing without insert
- [ ] POST /capture with extra field → 422
- [ ] POST /capture with neither url nor shared_text → 400
- [ ] `docker compose build ingest` succeeds with new deps + topics.yaml

- [ ] **Step 7.4: Push branch**

```bash
cd .. && git push -u origin feat/phase-3-capture-endpoint
```

---

## Spec coverage map

| Macro Phase 3 deliverable | Task |
|---|---|
| `topics.yaml` initial platforms map | 1 |
| `PlatformRouter` URL → platform | 2 |
| Pydantic request/response models | 3 |
| `db.py` asyncpg pool + 3 queries | 4 |
| Bearer auth dependency | 5 |
| `POST /capture` with auth + idempotency + stub | 6 |
| Tests for each layer | 1–6 |
| Build verification | 7 |

---

## Out of scope (Phase 4+)

- Worker that picks up `queued` rows and runs extraction → Phase 6
- Extractors (yt-dlp, markitdown) → Phase 4
- Classifier (LLM call to pick topic) → Phase 5
- GET/list/retry/delete endpoints → Phase 7
- Hot-reload of `topics.yaml` on mtime change → Phase 9
- Real metadata block in stub doc → Phase 5
- Anything that requires `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` → Phase 4/5
