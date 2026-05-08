# Phase 12: Browser extension for YouTube cookie sync

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture the last ~20% of YouTube content where Phase 11.5's transcript-api fallback fails (videos without captions, age-gated content, music videos with copyright holds). Solve it by giving cobalt + yt-dlp authenticated YouTube cookies via a tiny browser extension that the user installs once and forgets.

**Architecture:** The extension lives in the user's normal browser (Chrome/Firefox/Edge). It has the `cookies` host permission for `*.youtube.com`. On install + on cookie change + on a daily alarm, it builds a Netscape-format `cookies.txt` from the user's current YouTube session and POSTs it to a new `/youtube/cookies` endpoint on the ingest service. Ingest writes the cookie file atomically to a tmpfs volume shared with cobalt and the yt-dlp helper; both consume it on every request. Token-authenticated upload over HTTPS, write-only on the server side, never logged.

**Tech Stack:** Manifest V3 extension (works for Chrome 88+ AND Firefox 109+), vanilla JS service worker (no bundler — single ~150-line file), `chrome.cookies.getAll` API, `chrome.alarms` for the daily refresh, `chrome.storage.local` for the user-configured ingest URL + token. Server side: a single FastAPI POST endpoint (Python), `aiofiles` for atomic write, no DB changes.

**Cookie format:** [Netscape `cookies.txt`](https://curl.se/docs/http-cookies.html) — what yt-dlp's `--cookies` flag and cobalt's `YOUTUBE_COOKIES_FILE` both consume natively. Alternative would be JSON, but then we'd need a converter on the server; Netscape is the lingua franca.

**Security model:**
- HTTPS-only POST (rejected if request is HTTP and origin isn't `localhost`).
- Bearer token reused from `INGEST_API_TOKEN` (rotate to invalidate). Same token iOS already uses.
- Server-side: cookies written to a tmpfs-mounted directory (lost on container restart — extension auto-replenishes), `chmod 600`, atomic write via `os.rename` so consumers never see a half-written file.
- Cookies NEVER logged. Endpoint logs only `{ok: true, byte_count: N}`.
- Extension stores only the ingest URL + token in `chrome.storage.local` (encrypted at rest on Chrome; encrypted via OS keychain on Firefox).
- One file, one user, one workspace. No multi-tenant complexity in v1 — the extension doesn't know about Affine workspaces.

---

## File Structure

| File | Responsibility |
|---|---|
| `browser-extension/manifest.json` | NEW. MV3 manifest — declares `cookies` + host permissions, service worker, options page, icons. |
| `browser-extension/background.js` | NEW. Service worker. Listens for `cookies.onChanged`, runs daily alarm, builds Netscape format, POSTs to ingest. |
| `browser-extension/options.html` | NEW. Form: ingest URL + bearer token + "Sync now" button + last-sync status. |
| `browser-extension/options.js` | NEW. Reads/writes `chrome.storage.local`, triggers manual sync. |
| `browser-extension/popup.html` | NEW. Toolbar popup — shows last-sync timestamp + "Sync now" button. |
| `browser-extension/popup.js` | NEW. Popup → calls `chrome.runtime.sendMessage` to trigger sync. |
| `browser-extension/icons/` | NEW. 16/32/48/128 PNG icons. |
| `browser-extension/README.md` | NEW. Install instructions for unpacked-load (MV3 stores require store listing — out of scope for v1). |
| `ingest/src/api.py` | Add POST `/youtube/cookies` endpoint. |
| `ingest/src/youtube_cookies.py` | NEW. Cookie file storage helpers (atomic write, format validator). |
| `ingest/src/config.py` | Add `youtube_cookies_path` setting (default `/run/cookies/youtube.txt`). |
| `ingest/src/pipeline/extractors/_ytdlp_metadata.py` | Pass `--cookies $youtube_cookies_path` when the file exists. |
| `ingest/src/pipeline/extractors/_youtube_transcript.py` | Pass cookies to youtube-transcript-api when the file exists. |
| `compose.yaml` | Mount tmpfs at `/run/cookies` shared with cobalt; add `COBALT_YOUTUBE_COOKIES_FILE=/run/cookies/youtube.txt` env on the cobalt service. |
| `ingest/tests/test_youtube_cookies.py` | NEW. Endpoint + storage helper tests. |

---

## Task 1: Extension scaffold + manifest

**Files:**
- Create: `browser-extension/manifest.json`
- Create: `browser-extension/icons/icon-16.png`, `icon-32.png`, `icon-48.png`, `icon-128.png`
- Create: `browser-extension/README.md`

- [ ] **Step 1: manifest.json (MV3, Chrome + Firefox compatible)**

```json
{
  "manifest_version": 3,
  "name": "Affine YT Cookie Sync",
  "version": "0.1.0",
  "description": "Sync YouTube cookies to your self-hosted Affine ingest service so cobalt and yt-dlp can fetch authenticated content.",
  "permissions": ["cookies", "storage", "alarms"],
  "host_permissions": ["*://*.youtube.com/*", "https://*/*"],
  "background": { "service_worker": "background.js", "type": "module" },
  "options_ui": { "page": "options.html", "open_in_tab": false },
  "action": { "default_popup": "popup.html", "default_title": "YT Cookie Sync" },
  "icons": {
    "16": "icons/icon-16.png",
    "32": "icons/icon-32.png",
    "48": "icons/icon-48.png",
    "128": "icons/icon-128.png"
  },
  "browser_specific_settings": {
    "gecko": { "id": "yt-cookie-sync@affine.local", "strict_min_version": "109.0" }
  }
}
```

- [ ] **Step 2: README install steps**

Document the unpacked-load flow for both browsers (drag folder onto `chrome://extensions/` with Developer mode on; `about:debugging#/runtime/this-firefox` → Load Temporary Add-on for FF). Note that store distribution is out of scope for v1 — single-user tool.

- [ ] **Step 3: Placeholder icons**

Generate 4 PNG icons (16/32/48/128) with a simple cookie + sync arrow visual. Inkscape or any vector tool. Don't ship without icons — Chrome shows a generic puzzle piece otherwise.

---

## Task 2: Background service worker — cookie collection

**Files:**
- Create: `browser-extension/background.js`

- [ ] **Step 1: Define the sync function**

Reads cookies via `chrome.cookies.getAll({domain: 'youtube.com'})` AND `chrome.cookies.getAll({domain: '.youtube.com'})` (different cookie scopes). Dedupes by `(name, domain, path)`. Filters: keep `httpOnly`, `secure`, all session-relevant cookies. Drop cookies with names matching `__Secure-` patterns NOT in the YT auth set (avoid leaking unrelated cookies).

- [ ] **Step 2: Serialize to Netscape format**

```
# Netscape HTTP Cookie File
# This is a generated file!  Do not edit.

.youtube.com	TRUE	/	TRUE	1893456000	SID	<value>
```

Tab-separated fields: `domain`, `include_subdomains` (TRUE/FALSE), `path`, `secure` (TRUE/FALSE), `expires` (unix epoch; `0` for session cookies — yt-dlp tolerates), `name`, `value`. URL-decode values that came pre-encoded. Header lines start with `#`.

Helper function `cookiesToNetscape(cookies: chrome.cookies.Cookie[]): string`.

- [ ] **Step 3: POST to ingest**

```js
const { ingestUrl, ingestToken } = await chrome.storage.local.get(['ingestUrl', 'ingestToken']);
if (!ingestUrl || !ingestToken) return { ok: false, error: 'not configured' };

const body = cookiesToNetscape(cookies);
const resp = await fetch(`${ingestUrl}/youtube/cookies`, {
  method: 'POST',
  headers: {
    'Authorization': `Bearer ${ingestToken}`,
    'Content-Type': 'text/plain',
  },
  body,
});
```

Return `{ ok: resp.ok, byte_count: body.length, sync_at: new Date().toISOString() }`. Persist last-sync status to `chrome.storage.local` for the popup to read.

- [ ] **Step 4: Triggers**

```js
chrome.runtime.onInstalled.addListener(() => syncCookies());
chrome.cookies.onChanged.addListener(({ cookie }) => {
  if (cookie.domain.includes('youtube.com')) {
    debouncedSync();  // 30s debounce — cookies churn during navigation
  }
});
chrome.alarms.create('daily-sync', { periodInMinutes: 60 * 24 });
chrome.alarms.onAlarm.addListener(a => a.name === 'daily-sync' && syncCookies());
chrome.runtime.onMessage.addListener((msg, _, send) => {
  if (msg.type === 'sync-now') syncCookies().then(send);
  return true;  // async response
});
```

Debounce: store a pending timeout id in a top-level `let`; service workers do persist top-level state across short event bursts but die after ~30s idle. Use `chrome.alarms.create('debounce-sync', { delayInMinutes: 0.5 })` instead — survives worker death.

---

## Task 3: Options + popup UI

**Files:**
- Create: `browser-extension/options.html`, `options.js`
- Create: `browser-extension/popup.html`, `popup.js`

- [ ] **Step 1: options.html**

Form with: ingest URL (text input, e.g. `https://ingest.example.com:3200`), bearer token (password input), Save button. Below: "Last sync: <timestamp>", "Status: <ok|error>".

- [ ] **Step 2: options.js**

Load existing values from `chrome.storage.local` on DOMContentLoaded. Save via button click. Validate URL is HTTPS or localhost. Reject empty token.

- [ ] **Step 3: popup.html**

Toolbar popup — 320×200 px. Shows: "Last sync: <timestamp> ago", "Sync now" button, "Open settings" link.

- [ ] **Step 4: popup.js**

`Sync now` → `chrome.runtime.sendMessage({type: 'sync-now'})`. Shows result. Auto-closes 2s after success.

---

## Task 4: Ingest endpoint + storage

**Files:**
- Create: `ingest/src/youtube_cookies.py`
- Modify: `ingest/src/api.py` — add the POST endpoint
- Modify: `ingest/src/config.py` — add `youtube_cookies_path` setting

- [ ] **Step 1: Storage helpers**

`youtube_cookies.py`:

```python
def write_cookies_atomic(content: str, dest: Path) -> None:
    """Write cookies file atomically via os.rename. chmod 600."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.rename(tmp, dest)


def validate_netscape(content: str) -> tuple[bool, str | None]:
    """Quick validation: header line + at least one tab-separated 7-field row."""
    lines = [l for l in content.splitlines() if l.strip() and not l.startswith("#")]
    if not lines:
        return False, "no cookie rows"
    for line in lines:
        if line.count("\t") != 6:
            return False, f"row has {line.count('\\t')} tabs, expected 6"
    return True, None
```

- [ ] **Step 2: Add POST /youtube/cookies endpoint**

```python
@app.post("/youtube/cookies", status_code=204)
async def upload_youtube_cookies(
    request: Request,
    _: str = require_token,
) -> Response:
    raw = (await request.body()).decode("utf-8", errors="replace")
    ok, err = validate_netscape(raw)
    if not ok:
        raise HTTPException(status_code=400, detail=f"invalid cookies.txt: {err}")
    write_cookies_atomic(raw, Path(settings.youtube_cookies_path))
    log.info("youtube cookies uploaded", extra={"byte_count": len(raw)})
    return Response(status_code=204)
```

NEVER log the body. Only `byte_count`.

- [ ] **Step 3: Settings**

```python
youtube_cookies_path: str = "/run/cookies/youtube.txt"
```

`/run` is tmpfs by default in Docker — cookies don't survive container restart, which is fine because the extension auto-resyncs.

---

## Task 5: Wire cookies into yt-dlp + transcript-api + cobalt

**Files:**
- Modify: `ingest/src/pipeline/extractors/_ytdlp_metadata.py`
- Modify: `ingest/src/pipeline/extractors/_youtube_transcript.py`
- Modify: `compose.yaml` — mount shared volume, pass cobalt env
- Modify: `.env.example` — document `YOUTUBE_COOKIES_PATH`

- [ ] **Step 1: yt-dlp helper passes --cookies when file exists**

In `_run_ytdlp_metadata`, prepend the args list with `["--cookies", settings.youtube_cookies_path]` when `Path(settings.youtube_cookies_path).is_file()`. Don't pass when missing — yt-dlp errors on a missing cookies file. Log once at WARNING level on the FIRST capture without cookies so the user notices the extension isn't installed yet.

- [ ] **Step 2: transcript-api wires cookies**

youtube-transcript-api 1.x supports cookies via `YouTubeTranscriptApi(cookie_path=...)`. Update `_fetch_sync` to pass the path when the file exists:

```python
api_kwargs = {}
if Path(settings.youtube_cookies_path).is_file():
    api_kwargs["cookie_path"] = settings.youtube_cookies_path
ytt_api = YouTubeTranscriptApi(**api_kwargs)
```

Verify against the youtube-transcript-api docs at plan-execution time — the kwarg name may have shifted between minor versions.

- [ ] **Step 3: Cobalt service mounts the cookies volume**

Add to `compose.yaml`:

```yaml
volumes:
  yt_cookies:
    driver: local
    driver_opts:
      type: tmpfs
      device: tmpfs
      o: "size=1m,uid=1000"

services:
  ingest:
    volumes:
      - yt_cookies:/run/cookies
  cobalt:
    volumes:
      - yt_cookies:/cookies:ro
    environment:
      - COOKIE_PATH=/cookies/youtube.txt
```

Verify cobalt v11's exact env name for the cookie file at execution time — community forks vary.

- [ ] **Step 4: Document in .env.example**

```bash
# Path inside the ingest container where the YT cookie sync extension
# uploads its cookies.txt. Default is fine; only override if you remap
# the shared volume in compose.yaml.
# YOUTUBE_COOKIES_PATH=/run/cookies/youtube.txt
```

---

## Task 6: Tests

**Files:**
- Create: `ingest/tests/test_youtube_cookies.py`
- Modify: `ingest/tests/test_extractor_ytdlp.py` (add cookies-flag-passthrough case)

- [ ] **Step 1: Endpoint tests**

- 401 without bearer
- 400 on empty body
- 400 on malformed Netscape (wrong tab count)
- 204 on valid body, file is written with mode 0o600
- Body never appears in logs (capture caplog, assert no cookie names/values)

- [ ] **Step 2: Storage helper tests**

- `write_cookies_atomic` is atomic (interrupt mid-write → no partial file at dest)
- `validate_netscape` accepts header-only-comment + valid rows; rejects empty / bad tab counts

- [ ] **Step 3: yt-dlp passthrough test**

Mock `asyncio.create_subprocess_exec`. Verify `--cookies <path>` is in the args ONLY when the file exists. Use `tmp_path` fixture for the cookies file.

- [ ] **Step 4: transcript-api passthrough test**

Mock `YouTubeTranscriptApi.__init__` to capture kwargs. Verify `cookie_path` is passed when file exists, omitted when not.

---

## Task 7: Operational doc + README updates

**Files:**
- Modify: `README.md` — add "YouTube cookies" section under Ingest
- Create: `browser-extension/README.md` (already in Task 1, expand here)

- [ ] **Step 1: Stack README — install + verify**

Add a section explaining: install the extension (link to `browser-extension/`), set ingest URL + token in options, sync once, verify by tailing ingest logs for `youtube cookies uploaded`. Note that the cookies file is tmpfs — restart of the ingest container drops it, extension auto-reuploads on next YT visit (or wait until daily alarm).

- [ ] **Step 2: Browser-extension README**

Walk through the manifest, the Chrome/Firefox unpacked load steps, common errors (extension can't reach `https://localhost:3200` in some browser sandboxes — recommend using the LAN IP / external URL).

---

## Out of scope for v1

- **Multi-workspace support.** v1 has one ingest URL + one token in storage. Multi-workspace would need a small switcher.
- **Extension store distribution.** Chrome Web Store + Firefox AMO require review + a signed XPI. Manual unpacked install is fine for a personal tool.
- **Cookie staleness UI.** No "your cookies look stale, log in to YouTube" warning. The user notices when YT captures stop having transcripts; sync triggers on next YT page visit.
- **Per-video cookie scoping.** All cookies are sent for every YouTube domain — `accounts.google.com` cookies aren't scraped (would need separate host_permissions + scope), so very-restricted videos may still fail. Acceptable for v1; add later if needed.
- **Encryption-at-rest beyond what the browser provides.** Token sits in `chrome.storage.local`, which is only OS-level encrypted on Firefox and only in some Chrome configurations. For higher-stakes setups, recommend a dedicated browser profile.

---

## Risk assessment

| Risk | Likelihood | Mitigation |
|---|---|---|
| YouTube rotates auth cookies aggressively, sync churn | High | Already debounced 30s. `cookies.onChanged` only fires on actual change, not every page nav. Daily alarm is a safety net. |
| User installs extension but doesn't configure → silent failure | Medium | Background worker logs to `chrome://extensions` console + the popup shows "Not configured". Surface in the README. |
| Token leak via extension storage exfiltration (other malicious extension) | Low | Reuse `INGEST_API_TOKEN` so user can rotate without coordinating with iOS. Document the rotation procedure. |
| Server logs cookies by accident in some error path | Medium | Code review checkpoint before merge. Pytest assertion that body content never reaches `caplog`. |
| Cobalt env name for cookies file changes between v11.x | Medium | Pin cobalt version in compose.yaml. Document the env name lookup in the cobalt service comment. |
| Browser extension API changes (Manifest V4?) | Low (long horizon) | MV3 is the current standard through at least 2027. Re-evaluate at that point. |

---

## Acceptance criteria

- [ ] Extension installs cleanly on Chrome 120+ AND Firefox 120+ via unpacked load.
- [ ] After configuring + clicking "Sync now", `docker logs affine_ingest` shows `youtube cookies uploaded` with a `byte_count` field.
- [ ] A YT capture that previously failed at "Sign in to confirm you're not a bot" now succeeds end-to-end with a real Whisper transcript (or transcript-api captions).
- [ ] Killing the ingest container does NOT permanently break captures — extension auto-replenishes cookies on next YouTube tab activity.
- [ ] No cookie value or name appears anywhere in the ingest service's structured JSON logs.
- [ ] All new tests pass; full suite still green.
