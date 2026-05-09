# yt-dlp Video Bypass Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the cobalt-based Phase 13 video download with `yt-dlp + bgutil-ytdlp-pot-provider` (script mode), eliminating the headless-Chromium-via-residential-proxy fragility that's been blocking keyframe analysis since PR #36 added proxy support.

**Architecture:**
- `_video_download.download_video(url, workdir) -> Path` keeps its signature so `cobalt_ext._maybe_run_video_analysis` is untouched.
- New implementation shells out to the `yt-dlp` CLI via `asyncio.create_subprocess_exec` (mirroring [_ytdlp_metadata.py](ingest/src/pipeline/extractors/_ytdlp_metadata.py) which is already in production for metadata).
- yt-dlp inherits `HTTP_PROXY`/`HTTPS_PROXY` env vars (residential proxy) and reads the same cookies file already at `settings.youtube_cookies_path`.
- `bgutil-ytdlp-pot-provider` (script mode) supplies poToken via a pure-Node BgUtils subprocess — no headless Chromium, no separate HTTP service.
- cobalt remains in compose for the audio fallback path (rarely hit since captions cover ~80%). Only the VIDEO path stops touching it.

**Tech Stack:** Python 3.12, yt-dlp CLI, Node.js 20, `bgutil-ytdlp-pot-provider` (pip + npm), pytest-asyncio.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `ingest/pyproject.toml` | Modify | Add `bgutil-ytdlp-pot-provider` Python dep |
| `ingest/Dockerfile` | Modify | Install Node.js 20, clone + build bgutil server at `/opt/bgutil-pot/server` |
| `ingest/src/pipeline/extractors/_video_download.py` | Rewrite | yt-dlp subprocess invocation; same public signature |
| `ingest/tests/test_video_download.py` | Rewrite | Mock `asyncio.create_subprocess_exec` instead of httpx |

The four cobalt sidecar services (cobalt, cobalt_watchdog, yt_session_server, yt_session_adapter) stay in `compose.yaml` for now — they're still used by the cobalt audio fallback. A follow-up PR can remove them once we confirm the audio fallback path is also obsolete.

---

## Task 1: Add Python + Node deps

**Files:**
- Modify: `ingest/pyproject.toml` (line 22-23, dependencies block)
- Modify: `ingest/Dockerfile` (system deps, plus a new layer for Node + bgutil server)

- [ ] **Step 1: Add `bgutil-ytdlp-pot-provider` to pyproject.toml**

In the `dependencies = [...]` array, add:

```toml
    "bgutil-ytdlp-pot-provider>=1.0",
```

- [ ] **Step 2: Update Dockerfile to install Node 20 + bgutil server**

Replace the system-deps `apt-get install` line with one that adds nodejs and git, then add a new RUN to clone+build the bgutil server. After the `apt-get install` line, before `WORKDIR /app`:

```dockerfile
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates git gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# bgutil-ytdlp-pot-provider script mode: clone + build the BgUtils-based
# token generator. yt-dlp's `youtubepot-bgutilscript` plugin (pip-installed
# below) shells out to Node here to mint poToken on demand. Pure-JS BgUtils
# implementation — no headless Chromium needed, unlike the previous
# yt_session_server sidecar that this replaces.
ARG BGUTIL_VERSION=master
RUN git clone --depth 1 --branch ${BGUTIL_VERSION} \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider /opt/bgutil-pot \
    && cd /opt/bgutil-pot/server \
    && npm ci --omit=dev \
    && npx tsc \
    && rm -rf /root/.npm
```

- [ ] **Step 3: Commit**

```bash
git add ingest/pyproject.toml ingest/Dockerfile
git commit -m "$(cat <<'EOF'
feat(ingest): add yt-dlp poToken provider deps (bgutil script mode)

Adds bgutil-ytdlp-pot-provider Python plugin + clones the BgUtils-based
JS server to /opt/bgutil-pot/server in the Dockerfile. Prep for
replacing the cobalt video path with yt-dlp.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Failing test — happy path with mocked yt-dlp subprocess

**Files:**
- Modify: `ingest/tests/test_video_download.py` (full rewrite)

- [ ] **Step 1: Replace test file contents**

```python
"""Tests for yt-dlp video download (Phase 13 first stage).

Refactored from cobalt+httpx mocking to yt-dlp subprocess mocking after
the cobalt video tunnel proved unreliable (BotGuard via residential
proxy timed out reliably; see docs/plans/2026-05-09-yt-dlp-video-bypass.md).
"""

from __future__ import annotations

from pathlib import Path

import pytest


class _FakeProc:
    """Minimal stand-in for asyncio.subprocess.Process."""

    def __init__(self, returncode: int, stderr: bytes = b""):
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self):
        return b"", self._stderr

    def kill(self):  # called on timeout in the implementation
        pass


def _make_fake_exec(*, returncode: int, stderr: bytes = b"",
                    write_video_bytes: int | None = None,
                    output_arg_index: int = -2):
    """Returns a coroutine compatible with asyncio.create_subprocess_exec.

    When `write_video_bytes` is set, writes that many bytes to whatever
    output path yt-dlp was invoked with (the value of the `-o` arg).
    Mirrors what real yt-dlp does on success.
    """
    async def _fake_exec(*args, **_kwargs):
        if write_video_bytes is not None:
            # The yt-dlp CLI is invoked with `-o <path>`. Find it.
            argv = list(args)
            try:
                o_idx = argv.index("-o")
                out_path = Path(argv[o_idx + 1])
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(b"\x00" * write_video_bytes)
            except (ValueError, IndexError):
                pass
        return _FakeProc(returncode=returncode, stderr=stderr)
    return _fake_exec


@pytest.mark.asyncio
async def test_download_video_happy_path(monkeypatch, tmp_path: Path):
    """yt-dlp returncode 0 + a real file at the requested path → returns the path."""
    import asyncio
    from src.pipeline.extractors import _video_download as vd

    # 128 KB stub — well above MIN_VIDEO_BYTES (64 KB).
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_exec(returncode=0, write_video_bytes=128 * 1024),
    )

    out_path = await vd.download_video(
        "https://www.youtube.com/watch?v=abc",
        tmp_path,
    )
    assert out_path.exists()
    assert out_path.name == "video.mp4"
    assert out_path.stat().st_size == 128 * 1024


@pytest.mark.asyncio
async def test_download_video_subprocess_failure_raises(monkeypatch, tmp_path: Path):
    """Non-zero yt-dlp returncode → RuntimeError with stderr context."""
    import asyncio
    from src.pipeline.extractors import _video_download as vd

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_exec(
            returncode=1,
            stderr=b"ERROR: [youtube] abc: Sign in to confirm you're not a bot",
        ),
    )

    with pytest.raises(RuntimeError, match=r"yt-dlp video.*Sign in"):
        await vd.download_video(
            "https://www.youtube.com/watch?v=abc",
            tmp_path,
        )


@pytest.mark.asyncio
async def test_download_video_empty_file_raises_descriptive_error(
    monkeypatch, tmp_path: Path,
):
    """yt-dlp succeeded (rc=0) but produced a tiny / empty file. We treat
    this as failure with a poToken-pointing message — same defensive guard
    the previous cobalt impl had."""
    import asyncio
    from src.pipeline.extractors import _video_download as vd

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_exec(returncode=0, write_video_bytes=512),  # < 64 KB
    )

    with pytest.raises(RuntimeError, match=r"yt-dlp video too small.*poToken"):
        await vd.download_video(
            "https://www.youtube.com/watch?v=abc",
            tmp_path,
        )
    assert not (tmp_path / "video.mp4").exists()


@pytest.mark.asyncio
async def test_download_video_no_output_file_raises(monkeypatch, tmp_path: Path):
    """yt-dlp returned 0 but didn't write the expected file (corner case
    when yt-dlp's --output template doesn't match what we asked for, or
    permissions failure). Caller must see a clear error, not None."""
    import asyncio
    from src.pipeline.extractors import _video_download as vd

    # rc=0 but write_video_bytes=None → no file is written.
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_exec(returncode=0, write_video_bytes=None),
    )

    with pytest.raises(RuntimeError, match=r"yt-dlp video.*no output file"):
        await vd.download_video(
            "https://www.youtube.com/watch?v=abc",
            tmp_path,
        )


@pytest.mark.asyncio
async def test_download_video_passes_cookies_when_present(monkeypatch, tmp_path: Path):
    """When the cookies file exists (extension synced), the yt-dlp invocation
    must include `--cookies <path>`. Mirrors _ytdlp_metadata behavior."""
    import asyncio
    from src.pipeline.extractors import _video_download as vd
    from src.config import settings

    # Create a fake cookies file at the configured path.
    cookies_path = tmp_path / "youtube.txt"
    cookies_path.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setattr(settings, "youtube_cookies_path", str(cookies_path))

    captured: dict = {}

    async def _capturing_exec(*args, **_kwargs):
        captured["args"] = list(args)
        # Write a valid-sized file so the impl returns success.
        argv = list(args)
        o_idx = argv.index("-o")
        out_path = Path(argv[o_idx + 1])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"\x00" * (128 * 1024))
        return _FakeProc(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _capturing_exec)

    await vd.download_video("https://www.youtube.com/watch?v=abc", tmp_path)

    argv = captured["args"]
    assert "--cookies" in argv
    cookies_idx = argv.index("--cookies")
    assert argv[cookies_idx + 1] == str(cookies_path)


@pytest.mark.asyncio
async def test_download_video_passes_potoken_extractor_args(monkeypatch, tmp_path: Path):
    """The yt-dlp invocation must wire bgutil-ytdlp-pot-provider via
    --extractor-args pointing at /opt/bgutil-pot/server. Without that,
    yt-dlp won't find the script-mode provider and video format URLs 403."""
    import asyncio
    from src.pipeline.extractors import _video_download as vd

    captured: dict = {}

    async def _capturing_exec(*args, **_kwargs):
        captured["args"] = list(args)
        argv = list(args)
        o_idx = argv.index("-o")
        out_path = Path(argv[o_idx + 1])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"\x00" * (128 * 1024))
        return _FakeProc(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _capturing_exec)

    await vd.download_video("https://www.youtube.com/watch?v=abc", tmp_path)

    argv = captured["args"]
    # The exact form the docs prescribe for the script-mode provider.
    assert any(
        "youtubepot-bgutilscript:server_home=/opt/bgutil-pot/server" in a
        for a in argv
    ), f"missing bgutil extractor-args in: {argv}"
```

- [ ] **Step 2: Run tests — confirm they fail**

```bash
cd ingest && python -m pytest tests/test_video_download.py -v
```

Expected: All 6 tests fail (current `_video_download.py` is httpx-based, doesn't shell to yt-dlp).

---

## Task 3: Reimplement `_video_download.py` with yt-dlp subprocess

**Files:**
- Rewrite: `ingest/src/pipeline/extractors/_video_download.py`

- [ ] **Step 1: Replace file contents**

```python
"""yt-dlp video download (Phase 13 first stage).

Replaces the previous cobalt-based path. yt-dlp uses:
  - Cookies from settings.youtube_cookies_path (when present)
  - HTTP_PROXY/HTTPS_PROXY env vars (residential tunnel) — inherited
    by the subprocess automatically.
  - bgutil-ytdlp-pot-provider script mode for poToken generation. The
    plugin's BgUtils-based JS server lives at /opt/bgutil-pot/server
    (set up in the Dockerfile). Pure-Node — no Chromium, no proxy issues.

Returns the path to the downloaded mp4 (`workdir/video.mp4`). Raises
RuntimeError on yt-dlp failure or empty output. Caller (cobalt_ext's
_maybe_run_video_analysis) is responsible for cleanup.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from src.config import settings
from src.youtube_cookies import cookie_file_exists

log = logging.getLogger(__name__)


_DOWNLOAD_TIMEOUT_SECONDS = 300.0
_MIN_VIDEO_BYTES = 64 * 1024  # below this we treat the file as a failure
_BGUTIL_SERVER_HOME = "/opt/bgutil-pot/server"


async def download_video(url: str, workdir: Path) -> Path:
    """Download a 720p mp4 to `workdir/video.mp4` via yt-dlp.

    yt-dlp picks the best video+audio under 720p and merges them. The
    cap matches what we used for the cobalt path — high enough for
    keyframe analysis, low enough to keep tmpfs usage predictable.
    """
    out_path = workdir / "video.mp4"
    workdir.mkdir(parents=True, exist_ok=True)

    ytdlp_args: list[str] = [
        "yt-dlp",
        "--no-warnings",
        "--quiet",
        # 720p sweet spot — same as the previous cobalt path. yt-dlp picks
        # the best video <=720p + best audio and merges via ffmpeg.
        "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "--merge-output-format", "mp4",
        # bgutil-ytdlp-pot-provider script mode. The plugin discovers this
        # server_home via --extractor-args; without it yt-dlp falls back to
        # cookie-only auth and YT 403s the format URLs.
        "--extractor-args",
        f"youtubepot-bgutilscript:server_home={_BGUTIL_SERVER_HOME}",
    ]
    if cookie_file_exists(settings.youtube_cookies_path):
        ytdlp_args += ["--cookies", settings.youtube_cookies_path]

    ytdlp_args += ["-o", str(out_path), url]

    proc = await asyncio.create_subprocess_exec(
        *ytdlp_args,
        cwd=str(workdir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),  # inherit HTTP_PROXY/HTTPS_PROXY
    )
    try:
        _, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=_DOWNLOAD_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(
            f"yt-dlp video timed out after {_DOWNLOAD_TIMEOUT_SECONDS}s"
        ) from None

    if proc.returncode != 0:
        msg = stderr.decode(errors="replace")[:500].strip()
        raise RuntimeError(f"yt-dlp video failed (rc={proc.returncode}): {msg}")

    if not out_path.exists():
        raise RuntimeError(
            "yt-dlp video succeeded (rc=0) but no output file at "
            f"{out_path} — check yt-dlp output template / permissions"
        )

    size = out_path.stat().st_size
    if size < _MIN_VIDEO_BYTES:
        out_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"yt-dlp video too small: {size} bytes — most likely the "
            f"poToken provider failed (check bgutil-pot logs) or the "
            f"video has restrictions cookies don't unlock. Phase 13 "
            f"keyframes will be empty."
        )

    log.info(
        "yt-dlp video downloaded",
        extra={"byte_count": size, "path": str(out_path)},
    )
    return out_path
```

- [ ] **Step 2: Run tests**

```bash
cd ingest && python -m pytest tests/test_video_download.py -v
```

Expected: All 6 tests pass.

- [ ] **Step 3: Commit**

```bash
git add ingest/src/pipeline/extractors/_video_download.py ingest/tests/test_video_download.py
git commit -m "$(cat <<'EOF'
feat(ingest): replace cobalt video tunnel with yt-dlp + bgutil pot provider

Phase 13's video download now invokes yt-dlp via subprocess, mirroring
the existing _ytdlp_metadata pattern. yt-dlp inherits the residential
proxy env, reads the existing youtube cookies, and gets poToken from
the BgUtils-based script-mode provider built into the image.

Removes the dependency on cobalt + yt_session_server + adapter for
the video path (those services kept for the audio-fallback path for now).

Fixes the BotGuard-via-Chromium timeout that's been blocking inline
keyframe blocks since residential proxy was wired up (PR #36-#38).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Run full test suite

- [ ] **Step 1: Run all tests**

```bash
cd ingest && python -m pytest
```

Expected: 305+ tests pass (no regressions in cobalt_ext, video_analysis, orchestrator, etc.).

If any test fails because it imported `_TEST_TRANSPORT` from `_video_download`, fix by removing the import — `_TEST_TRANSPORT` no longer exists in the rewritten module.

- [ ] **Step 2: Push branch + open PR**

```bash
git push -u origin feat/yt-dlp-video-bypass
gh pr create --title "feat(ingest): yt-dlp + bgutil for Phase 13 video (drop cobalt video path)" --body "$(cat <<'EOF'
## Summary
- Replace cobalt video tunnel with yt-dlp subprocess + bgutil-ytdlp-pot-provider (script mode)
- Architectural fix for the BotGuard-via-headless-Chromium timeout in yt_session_server (13 PRs of cobalt fragility — see docs/plans/2026-05-09-yt-dlp-video-bypass.md)
- Reuses the residential proxy + cookies infrastructure already proven by the captions path; no new env vars

## Test plan
- [ ] `cd ingest && python -m pytest` — all 305+ tests green
- [ ] After deploy: capture a YouTube URL via /capture, confirm Phase 13 keyframes appear in the doc body (look for `## Keyframes` and `affine:image` blocks)
- [ ] `docker logs affine_ingest | grep "yt-dlp video downloaded"` — successful download log line on a fresh capture
- [ ] `docker logs affine_ingest | grep "video_analysis: complete"` — confirms the rest of Phase 13 runs cleanly post-download

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review checklist

- ✅ Spec coverage: all 4 file changes covered by tasks 1-3.
- ✅ No placeholders: every step has concrete code.
- ✅ Type consistency: `download_video(url, workdir) -> Path` signature unchanged from before; cobalt_ext's call site at `_maybe_run_video_analysis` keeps working.
- ✅ The 4 cobalt sidecars in compose.yaml are intentionally NOT touched — audio fallback still uses them. Cleanup is a follow-up PR after we confirm the audio path is also obsolete.
