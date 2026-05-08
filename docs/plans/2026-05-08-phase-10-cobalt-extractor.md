# Phase 10: Cobalt-based extractor

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `cobalt` extractor strategy so YouTube / Instagram / TikTok / X captures stop failing on yt-dlp's bot-detection blocks. Use the self-hosted [imputnet/cobalt](https://github.com/imputnet/cobalt) API as the media-fetch backend; transcribe its audio output via the existing Whisper pipeline.

**Architecture:** New `cobalt` Docker service runs alongside the stack. New `cobalt_ext.py` calls cobalt's JSON API for an audio tunnel URL, downloads the audio with `httpx`, runs it through Whisper (reusing the helper already in `ytdlp_ext.py`), and returns an `Extracted`. Routing in `topics.yaml` switches the bot-blocked platforms (YouTube, Instagram, TikTok, X, Vimeo) from `ytdlp` to `cobalt`. yt-dlp stays registered for everything else (Reddit, arXiv, podcasts, articles fall through to their existing extractors anyway).

**Tech Stack:** cobalt v11 (Node, ghcr.io/imputnet/cobalt:11), Python `httpx` for cobalt API + audio download, OpenAI Whisper API (already in use), pytest with `httpx.MockTransport`.

**Cobalt API contract used (v11):**
- `POST {COBALT_API_URL}/` with `Content-Type: application/json`, `Accept: application/json`, body `{"url": "<target>", "downloadMode": "audio", "audioFormat": "m4a"}`.
- Success → `{"status": "tunnel"|"redirect", "url": "<download-url>", "filename": "<name>"}`.
- Audio-only platforms / "audio" downloadMode that returns a video-with-audio stream still works — cobalt re-muxes.
- Error → `{"status": "error", "error": {"code": "...", "context": "..."}}` (treated as extraction failure).

---

## File Structure

| File | Responsibility |
|---|---|
| `compose.yaml` | Add `cobalt` service. Add `COBALT_API_URL` to ingest's env. |
| `.env.example` | Document `COBALT_API_URL` (default `http://cobalt:9000`). |
| `ingest/src/config.py` | Add `cobalt_api_url` setting. |
| `ingest/src/pipeline/extractors/cobalt_ext.py` | NEW. The extractor. |
| `ingest/src/pipeline/extractors/__init__.py` | Side-effect import of `cobalt_ext`. |
| `ingest/topics.yaml` | Route youtube/instagram/tiktok/x/vimeo → `cobalt`. Leave catch-all + reddit/arxiv/podcasts as-is. |
| `ingest/tests/test_extractor_cobalt.py` | NEW. Unit tests with mocked cobalt API + Whisper. |

---

## Task 1: Add cobalt service to compose.yaml

**Files:**
- Modify: `compose.yaml` (add new service block; modify `ingest:` env block)

- [ ] **Step 1: Add the cobalt service block above the `ingest:` service**

Insert just before the `ingest_migration:` block (line ~213):

```yaml
  # imputnet/cobalt — media downloader API. Replaces direct yt-dlp use
  # for platforms that aggressively bot-detect (YouTube, Instagram,
  # TikTok, X). The cobalt project tracks the platform-fight upstream
  # so we don't have to maintain cookie jars or POT solvers ourselves.
  # Internal-only by default — accessed by the ingest worker over the
  # affine_net network.
  cobalt:
    image: ghcr.io/imputnet/cobalt:11
    container_name: affine_cobalt
    environment:
      # API_URL is what cobalt advertises in CORS / response URLs. For
      # internal-only use the docker-network URL is fine; switch to a
      # public URL only if you also publish the port + put it behind a
      # reverse proxy.
      - API_URL=http://cobalt:9000/
      - DURATION_LIMIT=${COBALT_DURATION_LIMIT:-10800}
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://localhost:9000/"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 10s
    networks:
      - affine_net
```

- [ ] **Step 2: Wire `COBALT_API_URL` into the ingest service env**

In `compose.yaml`, modify the ingest service `environment:` block (~line 250) to add the cobalt URL right after `MCP_EXT_URL`:

```yaml
      - MCP_EXT_URL=http://mcp_ext:3100
      - COBALT_API_URL=${COBALT_API_URL:-http://cobalt:9000}
```

- [ ] **Step 3: Add `cobalt:` to the ingest service's `depends_on:`**

So ingest waits for cobalt to be healthy before starting its worker:

```yaml
    depends_on:
      ingest_migration:
        condition: service_completed_successfully
      mcp_ext:
        condition: service_healthy
      postgres:
        condition: service_healthy
      cobalt:
        condition: service_healthy
```

- [ ] **Step 4: Verify the YAML still parses**

```bash
docker compose -f compose.yaml config --quiet && echo OK
```

Expected: `OK` (no output before it).

- [ ] **Step 5: Commit**

```bash
git add compose.yaml
git commit -m "feat(stack): add cobalt service for media extraction

imputnet/cobalt:11 sidecar. Replaces direct yt-dlp use for the platforms
that bot-detect (YouTube, Instagram, TikTok, X) — cobalt's maintainers
chase the platform-fight upstream so we don't have to ship cookie jars
or POT solvers in our image."
```

---

## Task 2: Document `COBALT_API_URL` in .env.example

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Add a cobalt section after the existing ingest env block**

Append at the bottom of `.env.example`:

```
# === Cobalt (media extractor) ===============================================
# Self-hosted imputnet/cobalt API. Default URL is the in-stack hostname;
# only override if you publish the cobalt port and want to call it from
# outside docker.
COBALT_API_URL=http://cobalt:9000

# Per-request duration cap in seconds. cobalt rejects media longer than
# this. Default 10800 = 3h, enough for podcasts; trim if you want to
# bound Whisper spend.
COBALT_DURATION_LIMIT=10800
```

- [ ] **Step 2: Commit**

```bash
git add .env.example
git commit -m "docs(stack): document COBALT_API_URL + COBALT_DURATION_LIMIT"
```

---

## Task 3: Add `cobalt_api_url` setting

**Files:**
- Modify: `ingest/src/config.py` (Settings dataclass)
- Test: `ingest/tests/test_config.py` (if it exists; otherwise skip the test step — config is plain pydantic-settings and a dedicated unit test for one env mapping is overkill)

- [ ] **Step 1: Add the new setting to the `Settings` class**

In `ingest/src/config.py`, in the `Settings` class, add after `max_body_chars`:

```python
    cobalt_api_url: str = "http://cobalt:9000"
    cobalt_duration_limit: int = 10800
```

- [ ] **Step 2: Verify pydantic-settings picks it up**

Run a smoke check:

```bash
ingest/.venv/bin/python -c "from src.config import settings; print(settings.cobalt_api_url, settings.cobalt_duration_limit)" 
```

(Run from inside `ingest/`.)
Expected: `http://cobalt:9000 10800`.

- [ ] **Step 3: Commit**

```bash
git add ingest/src/config.py
git commit -m "feat(ingest): add cobalt_api_url + cobalt_duration_limit settings"
```

---

## Task 4: Write failing tests for cobalt extractor

**Files:**
- Create: `ingest/tests/test_extractor_cobalt.py`

- [ ] **Step 1: Write the test file**

```python
"""Unit tests for the cobalt extractor.

Mocks at three boundaries:
  - cobalt API (httpx.MockTransport)
  - audio download (httpx.MockTransport, same client)
  - Whisper transcription (monkeypatched _whisper_transcribe)

Real cobalt + real Whisper integration belongs in test_extractors_integration.py
behind the `integration` marker; these tests stay hermetic.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from src.config import Platform
from src.pipeline.extracted import MediaKind


def _platform(id_: str = "youtube") -> Platform:
    return Platform(
        id=id_,
        group="Socials",
        folder_name="Youtube",
        hosts=["youtube.com"],
        extractor="cobalt",
    )


@pytest.mark.asyncio
async def test_cobalt_happy_path_returns_transcript(monkeypatch, tmp_path):
    from src.pipeline.extractors import cobalt_ext

    # 1. Mock cobalt API: returns a tunnel URL.
    # 2. Mock the tunnel URL: returns 32 bytes of fake audio.
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/" and request.method == "POST":
            body = json.loads(request.content)
            assert body["url"] == "https://www.youtube.com/watch?v=abc"
            assert body["downloadMode"] == "audio"
            return httpx.Response(
                200,
                json={"status": "tunnel", "url": "http://cobalt:9000/tunnel/xyz", "filename": "sample.m4a"},
            )
        if request.url.path.startswith("/tunnel/"):
            return httpx.Response(200, content=b"\x00" * 32)
        return httpx.Response(404)

    transport = httpx.MockTransport(_handler)
    monkeypatch.setattr(cobalt_ext, "_TEST_TRANSPORT", transport, raising=False)

    # 3. Mock Whisper.
    monkeypatch.setattr(
        cobalt_ext,
        "_whisper_transcribe",
        AsyncMock(return_value="hello world this is a transcript"),
    )

    result = await cobalt_ext.extract(
        "https://www.youtube.com/watch?v=abc",
        _platform(),
    )

    assert result.media_kind == MediaKind.VIDEO
    assert "hello world" in result.body_md
    assert result.extra["extractor"] == "cobalt"
    assert result.extra["platform_id"] == "youtube"


@pytest.mark.asyncio
async def test_cobalt_redirect_status_treated_like_tunnel(monkeypatch):
    """cobalt sometimes returns status=redirect (direct CDN URL) — same shape."""
    from src.pipeline.extractors import cobalt_ext

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"status": "redirect", "url": "http://cdn.example.com/audio.m4a", "filename": "a.m4a"},
            )
        return httpx.Response(200, content=b"\x00" * 16)

    monkeypatch.setattr(cobalt_ext, "_TEST_TRANSPORT", httpx.MockTransport(_handler), raising=False)
    monkeypatch.setattr(cobalt_ext, "_whisper_transcribe", AsyncMock(return_value="ok"))

    result = await cobalt_ext.extract("https://example.com/x", _platform())
    assert "ok" in result.body_md


@pytest.mark.asyncio
async def test_cobalt_error_status_raises_runtime_error(monkeypatch):
    from src.pipeline.extractors import cobalt_ext

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "error", "error": {"code": "fetch.empty", "context": ""}},
        )

    monkeypatch.setattr(cobalt_ext, "_TEST_TRANSPORT", httpx.MockTransport(_handler), raising=False)

    with pytest.raises(RuntimeError, match="cobalt error.*fetch.empty"):
        await cobalt_ext.extract("https://example.com/x", _platform())


@pytest.mark.asyncio
async def test_cobalt_http_error_raises_runtime_error(monkeypatch):
    from src.pipeline.extractors import cobalt_ext

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"status": "error", "error": {"code": "down"}})

    monkeypatch.setattr(cobalt_ext, "_TEST_TRANSPORT", httpx.MockTransport(_handler), raising=False)

    with pytest.raises(RuntimeError, match="cobalt http"):
        await cobalt_ext.extract("https://example.com/x", _platform())
```

- [ ] **Step 2: Run the tests to verify they fail (no implementation yet)**

```bash
cd ingest && .venv/bin/pytest tests/test_extractor_cobalt.py -v
```

Expected: ImportError or AttributeError because `cobalt_ext` doesn't exist yet.

- [ ] **Step 3: Commit the failing tests**

```bash
git add ingest/tests/test_extractor_cobalt.py
git commit -m "test(ingest): cobalt extractor — happy path, redirect, errors"
```

---

## Task 5: Implement the cobalt extractor

**Files:**
- Create: `ingest/src/pipeline/extractors/cobalt_ext.py`

- [ ] **Step 1: Write the extractor module**

Create `ingest/src/pipeline/extractors/cobalt_ext.py`:

```python
"""Cobalt-based media extractor.

Pipeline:
    1. POST {COBALT_API_URL}/ with the target URL, downloadMode=audio.
    2. Parse the tunnel/redirect URL out of the response.
    3. Stream the audio to a temp file.
    4. Run it through Whisper (reusing ytdlp_ext._whisper_transcribe).
    5. Return Extracted with body_md = the transcript.

Why no rich metadata: cobalt doesn't return reliable title/channel/
description. The iOS client already provides `shared_title`, which the
caller (api.py) uses for the AFFiNE doc title — so leaving title=None
in Extracted doesn't lose anything observable to the user. Author/
published_at are None for the same reason.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import httpx

from src.config import Platform, settings
from src.pipeline.extracted import Extracted, MediaKind, truncate_body
from src.pipeline.extractors import register_extractor
# Reuse the Whisper helper from the ytdlp extractor — same OpenAI key,
# same client wiring, and the function is small and self-contained.
from src.pipeline.extractors.ytdlp_ext import _whisper_transcribe


# Tests inject a MockTransport here. Production leaves it None so httpx
# uses the default networking transport.
_TEST_TRANSPORT: httpx.AsyncBaseTransport | None = None


_COBALT_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)


async def extract(url: str, platform: Platform) -> Extracted:
    workdir = Path(tempfile.mkdtemp(prefix="ingest-cobalt-", dir="/tmp/ingest"))
    try:
        tunnel_url = await _request_tunnel(url)
        audio_path = await _download_audio(tunnel_url, workdir)
        transcript = await _whisper_transcribe(audio_path)

        body_parts = [f"# {url}", "", "## Transcript (Whisper via cobalt)", "", transcript]
        body_md = truncate_body("\n".join(body_parts), limit=settings.max_body_chars)

        return Extracted(
            title=None,
            body_md=body_md,
            author=None,
            published_at=None,
            media_kind=MediaKind.VIDEO,
            extra={
                "extractor": "cobalt",
                "platform_id": platform.id,
                "tunnel_url": tunnel_url,
            },
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _request_tunnel(url: str) -> str:
    payload = {
        "url": url,
        "downloadMode": "audio",
        "audioFormat": "m4a",
    }
    async with _client() as client:
        try:
            resp = await client.post("/", json=payload)
        except httpx.HTTPError as e:
            raise RuntimeError(f"cobalt http: {type(e).__name__}: {e}") from e

        if resp.status_code >= 400:
            raise RuntimeError(f"cobalt http: status={resp.status_code} body={resp.text[:200]}")

        body = resp.json()

    status = body.get("status")
    if status in ("tunnel", "redirect"):
        tunnel = body.get("url")
        if not tunnel:
            raise RuntimeError(f"cobalt response missing url: {body}")
        return tunnel
    if status == "error":
        err = body.get("error", {}) or {}
        code = err.get("code", "unknown")
        ctx = err.get("context", "")
        raise RuntimeError(f"cobalt error: {code} {ctx}".strip())
    if status == "picker":
        # Multi-asset response — pick the first audio entry if present.
        audio_url = body.get("audio")
        if audio_url:
            return audio_url
        raise RuntimeError(f"cobalt picker response had no audio: {body}")

    raise RuntimeError(f"cobalt unexpected status: {status} body={body}")


async def _download_audio(tunnel_url: str, workdir: Path) -> Path:
    out_path = workdir / "audio.m4a"
    async with _client() as client:
        async with client.stream("GET", tunnel_url) as resp:
            if resp.status_code >= 400:
                raise RuntimeError(f"cobalt download: status={resp.status_code}")
            with out_path.open("wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
    return out_path


def _client() -> httpx.AsyncClient:
    kwargs = {
        "base_url": settings.cobalt_api_url.rstrip("/"),
        "timeout": _COBALT_TIMEOUT,
        "headers": {
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    }
    if _TEST_TRANSPORT is not None:
        kwargs["transport"] = _TEST_TRANSPORT
    return httpx.AsyncClient(**kwargs)


register_extractor("cobalt", extract)
```

- [ ] **Step 2: Register the new module in the extractors package**

Modify `ingest/src/pipeline/extractors/__init__.py` — add `cobalt_ext` to the side-effect import block:

```python
# Side-effect imports register the four built-ins.
from src.pipeline.extractors import (  # noqa: E402, F401
    markitdown_ext,
    ytdlp_ext,
    oembed_ytdlp_ext,
    reddit_json_ext,
    cobalt_ext,
)
```

- [ ] **Step 3: Run the cobalt tests — they should pass**

```bash
cd ingest && .venv/bin/pytest tests/test_extractor_cobalt.py -v
```

Expected: all 4 cases pass.

- [ ] **Step 4: Run the full test suite — nothing should regress**

```bash
cd ingest && .venv/bin/pytest -q
```

Expected: all tests pass; integration tests (5 currently) skipped.

- [ ] **Step 5: Commit**

```bash
git add ingest/src/pipeline/extractors/cobalt_ext.py ingest/src/pipeline/extractors/__init__.py
git commit -m "feat(ingest): cobalt extractor — tunnel + Whisper transcript

Calls cobalt's /api endpoint for an audio tunnel URL, streams the audio
to a temp file, runs it through Whisper (reusing the helper from
ytdlp_ext), and returns Extracted. Title/author/published_at left None
because cobalt's response metadata isn't reliable; the iOS client
provides shared_title for the AFFiNE doc title and the classifier reads
body_md (the transcript), which is what we actually need."
```

---

## Task 6: Route bot-blocked platforms through cobalt in topics.yaml

**Files:**
- Modify: `ingest/topics.yaml`

- [ ] **Step 1: Switch the four bot-blocked platforms to `cobalt`**

In `ingest/topics.yaml`, change the `extractor:` field on these platforms from `ytdlp` (or `oembed_ytdlp` for x) to `cobalt`:

- `youtube`: `extractor: ytdlp` → `extractor: cobalt`
- `instagram`: `extractor: ytdlp` → `extractor: cobalt`
- `tiktok`: `extractor: ytdlp` → `extractor: cobalt`
- `x`: `extractor: oembed_ytdlp` → `extractor: cobalt`
- `vimeo`: `extractor: ytdlp` → `extractor: cobalt`

Leave `reddit`, `arxiv`, `podcast_apple`, `spotify_episode`, and the catch-all `article` untouched — those use `reddit_json` / `markitdown` and don't get bot-blocked.

- [ ] **Step 2: Verify topics.yaml still parses**

```bash
cd ingest && .venv/bin/python -c "from src.config import load_topics; print([p.id + '=' + p.extractor for p in load_topics().platforms])"
```

Expected output should show e.g. `youtube=cobalt`, `instagram=cobalt`, `tiktok=cobalt`, `x=cobalt`, `vimeo=cobalt`.

- [ ] **Step 3: Run the topics-config tests**

```bash
cd ingest && .venv/bin/pytest tests/test_topics_config.py -v
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add ingest/topics.yaml
git commit -m "feat(ingest): route YouTube/Instagram/TikTok/X/Vimeo through cobalt

These five platforms aggressively block direct yt-dlp scraping. Cobalt
handles the cookie/POT/fingerprint dance upstream. Reddit/arXiv/podcasts
stay on their existing extractors (no bot-block issue there)."
```

---

## Task 7: Final verification + push

- [ ] **Step 1: Run the full test suite one more time**

```bash
cd ingest && .venv/bin/pytest -q
```

Expected: all unit tests pass.

- [ ] **Step 2: Push the branch**

```bash
git push stheno fix/capture-error-envelope-and-deep-health
```

(The fix branch from the previous PR is reused — same logical follow-up. If you'd rather have an independent PR, branch off main instead and push that.)

- [ ] **Step 3: After merge + redeploy, smoke-test from outside**

```bash
# /health/deep should be all-green:
curl -s https://ingest.xcrux.team/health/deep | jq

# Submit a YouTube link (real capture — pick a short video!):
curl -s -X POST https://ingest.xcrux.team/capture \
  -H "Authorization: Bearer $INGEST_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ","shared_title":"Smoke test","source_app":null,"shared_text":null}' | jq

# Watch the worker pick it up; status should walk through queued →
# extracting → classifying → filing → done within ~30s for a short video.
curl -s "https://ingest.xcrux.team/captures/<id>" \
  -H "Authorization: Bearer $INGEST_API_TOKEN" | jq
```

Expected: capture reaches `status: "done"`. The doc in AFFiNE has the Whisper transcript appended.

---

## Self-Review

**Spec coverage:**
- Cobalt service in compose ✓ (Task 1)
- Env vars wired in ✓ (Task 1, 2)
- Settings exposed ✓ (Task 3)
- Extractor module + tests ✓ (Tasks 4, 5)
- Routing change ✓ (Task 6)
- Verification ✓ (Task 7)

**Placeholder scan:** none.

**Type consistency:** `extract` signature matches the registered `ExtractFunc` type. `Extracted` fields used (`title`, `body_md`, `author`, `published_at`, `media_kind`, `extra`) all exist in `extracted.py`. `Platform` import path matches existing extractors. `_whisper_transcribe` import works because Python module-level functions are addressable across files.

**Open risks acknowledged in plan body:**
- cobalt's `picker` response shape is handled but never been seen in practice for audio mode.
- No metadata in `Extracted.title/author/published_at` — acceptable for v1, can revisit by adding an oEmbed pre-fetch in Phase 10.5 if classifier accuracy degrades.
