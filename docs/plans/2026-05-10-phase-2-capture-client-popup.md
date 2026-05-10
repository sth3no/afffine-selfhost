# Phase 2: Capture API client + popup capture flow

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** From the toolbar popup, on any tab, the user can click "Save to AFFiNE" and a row appears server-side via `POST /capture`. The popup mirrors the iOS Share Extension UI (see spec §4.1): ⏳ Capturing → ✓ Saved · Open ↗ → auto-close. Failures show a red status with retry / settings deep-link.

**Spec:** [`docs/specs/2026-05-10-browser-extension-multitool-design.md`](../specs/2026-05-10-browser-extension-multitool-design.md) §4.1, §6.1, §7

**Macro plan:** Phase 2 in [`docs/plans/2026-05-10-browser-extension-multitool-macro-plan.md`](2026-05-10-browser-extension-multitool-macro-plan.md)

**Architecture:** The popup talks to the background service worker (`chrome.runtime.sendMessage({type:'capture', payload})`); the background routes to `capture/client.js` which calls `lib/api.js`. Result lands in `chrome.storage.local.lastResult` + prepended into `recentCaptures` cache so future surfaces (Phase 6 History tab) see it.

**Server contract** (per [docs/specs/2026-05-06-ingest-service-design.md](../specs/2026-05-06-ingest-service-design.md) and `portainer-stack/ingest/src/models.py`):

`POST /capture` request:
```json
{ "url": "https://...", "source_app": "youtube.com", "shared_title": "...", "shared_text": "..." }
```
At least one of `url`/`shared_text` required. `source_app`/`shared_title`/`shared_text` are all optional.

`POST /capture` response (202):
```json
{ "capture_id": "01J...", "doc_id": "...", "web_url": "https://...", "status": "queued",
  "platform": "youtube", "initial_path": "Sources/Videos/YouTube", "created_at": "..." }
```

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `browser-extension/capture/client.js` | Create | Typed wrappers for `POST /capture`, `GET /captures`, `GET /captures/{id}`, `POST /captures/{id}/retry`, `DELETE /captures/{id}`. Uses `lib/api.js`. |
| `browser-extension/capture/payload.js` | Create | `buildPayloadFromTab(tab, info?)` — pure function: tab + optional context-menu info → `CaptureRequest`-shaped object. |
| `browser-extension/capture/__tests__/payload.test.js` | Create | Unit tests: popup, link, selection, image inputs → expected payload. |
| `browser-extension/background.js` | Modify | Add `chrome.runtime.onMessage` handler for `{type:'capture', payload}` — calls `client.captureUrl`, persists `lastResult`, updates `recentCaptures` cache. |
| `browser-extension/popup/popup.html` | Rewrite | New layout: header (favicon + title) + primary button + status row + cookie footer. |
| `browser-extension/popup/popup.css` | Create | Extracted from v0.1 inline styles, restyled per design tokens. |
| `browser-extension/popup/popup.js` | Rewrite | On click → query active tab → `buildPayloadFromTab` → `sendMessage('capture', ...)` → render result + auto-close. |

---

## Task 1: `capture/client.js` + `capture/payload.js` + tests

**Files:**
- Create: `browser-extension/capture/client.js`
- Create: `browser-extension/capture/payload.js`
- Create: `browser-extension/capture/__tests__/payload.test.js`

- [ ] **Step 1: Failing test for `buildPayloadFromTab`**

`capture/__tests__/payload.test.js`:

```js
/** @vitest-environment node */
import { describe, it, expect } from 'vitest';
import { buildPayloadFromTab } from '../payload.js';

describe('capture/payload.buildPayloadFromTab', () => {
  const tab = { url: 'https://www.youtube.com/watch?v=abc', title: 'Some video' };

  it('popup capture uses tab URL + title + hostname source_app', () => {
    expect(buildPayloadFromTab(tab)).toEqual({
      url: 'https://www.youtube.com/watch?v=abc',
      source_app: 'www.youtube.com',
      shared_title: 'Some video',
    });
  });

  it('link capture (info.linkUrl) uses link URL, NOT page URL', () => {
    const info = { linkUrl: 'https://example.com/article', selectionText: undefined };
    expect(buildPayloadFromTab(tab, info)).toEqual({
      url: 'https://example.com/article',
      source_app: 'www.youtube.com',  // host page is still the source
      shared_title: 'Some video',
    });
  });

  it('selection capture (info.selectionText) sends shared_text + page URL', () => {
    const info = { selectionText: 'A great quote.', linkUrl: undefined };
    expect(buildPayloadFromTab(tab, info)).toEqual({
      url: 'https://www.youtube.com/watch?v=abc',
      source_app: 'www.youtube.com',
      shared_title: 'Some video',
      shared_text: 'A great quote.',
    });
  });

  it('image capture (info.srcUrl) uses image URL', () => {
    const info = { srcUrl: 'https://cdn.example.com/img.png' };
    expect(buildPayloadFromTab(tab, info)).toEqual({
      url: 'https://cdn.example.com/img.png',
      source_app: 'www.youtube.com',
      shared_title: 'Some video',
    });
  });

  it('omits undefined shared_title gracefully', () => {
    const stripped = { url: tab.url, title: undefined };
    const out = buildPayloadFromTab(stripped);
    expect(out.shared_title).toBeUndefined();
    expect(out.url).toBe(tab.url);
  });

  it('handles non-http tab URL (chrome://) by leaving source_app empty', () => {
    const internal = { url: 'chrome://newtab/', title: 'New tab' };
    expect(buildPayloadFromTab(internal)).toEqual({
      url: 'chrome://newtab/',
      source_app: null,
      shared_title: 'New tab',
    });
  });
});
```

- [ ] **Step 2: Run — fails**

```sh
npm test -- capture/__tests__/payload.test.js
```

Expected: FAIL — Cannot find module.

- [ ] **Step 3: Implement `capture/payload.js`**

```js
/**
 * Build a CaptureRequest-shaped payload from a chrome.tabs.Tab and optional
 * contextMenus.OnClickData info. The shape matches the server's Pydantic
 * model: { url, source_app?, shared_title?, shared_text? }.
 *
 * Precedence for `url`:
 *   1. info.linkUrl   (right-click "Save link")
 *   2. info.srcUrl    (right-click "Save image")
 *   3. tab.url        (popup / context-menu page / context-menu selection)
 *
 * `shared_text` only set when info.selectionText is non-empty.
 * `source_app` derived from tab.url's hostname; null for non-http(s) URLs.
 */
export function buildPayloadFromTab(tab, info = {}) {
  const url = info.linkUrl ?? info.srcUrl ?? tab.url;
  const sourceApp = hostnameOrNull(tab.url);
  const payload = {
    url,
    source_app: sourceApp,
    shared_title: tab.title ?? undefined,
  };
  if (info.selectionText) {
    payload.shared_text = info.selectionText;
  }
  return payload;
}

function hostnameOrNull(urlStr) {
  try {
    const u = new URL(urlStr);
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
    return u.hostname || null;
  } catch {
    return null;
  }
}
```

- [ ] **Step 4: Run — passes**

```sh
npm test -- capture/__tests__/payload.test.js
```

Expected: 6 / 6 passing.

- [ ] **Step 5: Implement `capture/client.js`**

```js
/**
 * Capture API client. Thin wrappers over lib/api.js for the five capture-side
 * endpoints:
 *
 *   POST   /capture
 *   GET    /captures?limit=&status=&cursor=
 *   GET    /captures/{id}
 *   POST   /captures/{id}/retry
 *   DELETE /captures/{id}
 *
 * Each method returns the parsed JSON; lib/api.js maps non-2xx into IngestError.
 */
import { request } from '../lib/api.js';

/**
 * @param {{url?: string, source_app?: string|null, shared_title?: string,
 *           shared_text?: string}} payload
 * @returns {Promise<{capture_id: string, doc_id: string, web_url: string,
 *           status: string, platform: string, initial_path: string,
 *           created_at: string}>}
 */
export async function captureUrl(payload) {
  return await request('POST', '/capture', { body: payload });
}

export async function listCaptures({ limit = 50, status, cursor } = {}) {
  const params = new URLSearchParams();
  params.set('limit', String(limit));
  if (status) params.set('status', status);
  if (cursor) params.set('cursor', cursor);
  return await request('GET', `/captures?${params}`);
}

export async function getCapture(id) {
  return await request('GET', `/captures/${encodeURIComponent(id)}`);
}

export async function retryCapture(id) {
  return await request('POST', `/captures/${encodeURIComponent(id)}/retry`);
}

export async function deleteCapture(id) {
  return await request('DELETE', `/captures/${encodeURIComponent(id)}`);
}
```

- [ ] **Step 6: Commit**

```sh
git add browser-extension/capture/
git commit -m "feat(extension): capture/* client + payload builder + tests (phase 2.1)"
```

---

## Task 2: `background.js` capture message handler

**Files:**
- Modify: `browser-extension/background.js`

- [ ] **Step 1: Extend the `chrome.runtime.onMessage` listener**

Replace the existing `chrome.runtime.onMessage.addListener` block (Task 6 of Phase 1) to add a new `'capture'` branch. The full new listener:

```js
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === 'sync-now') {
    syncCookies().then(sendResponse);
    return true;
  }
  if (msg?.type === 'get-last-sync') {
    import('./lib/storage.js').then(m => m.getLastSync()).then(sendResponse);
    return true;
  }
  if (msg?.type === 'capture') {
    handleCapture(msg.payload).then(sendResponse);
    return true;
  }
});
```

- [ ] **Step 2: Add the `handleCapture` function and its imports**

Add to the top of `background.js` (next to the existing `cookies/sync.js` and `lib/badge.js` imports):

```js
import { captureUrl } from './capture/client.js';
import { setLastResult, getRecentCaptures, setRecentCaptures } from './lib/storage.js';
import { setSubsystem } from './lib/badge.js';
import { IngestError } from './lib/api.js';
```

Add the `handleCapture` function below the listener registrations (before the file ends):

```js
/**
 * Capture flow: POST /capture, persist lastResult + prepend to recentCaptures
 * cache, update capture-subsystem badge.
 */
async function handleCapture(payload) {
  try {
    const response = await captureUrl(payload);
    const result = { ok: true, ...response };
    await setLastResult(result);
    const recent = await getRecentCaptures();
    // Prepend (newest first); setRecentCaptures truncates to 50.
    await setRecentCaptures([toRecentRow(response), ...recent]);
    await setSubsystem('capture', 'ok');
    return result;
  } catch (e) {
    const err = e instanceof IngestError
      ? { kind: e.kind, message: e.message, status: e.status }
      : { kind: 'unknown', message: String(e) };
    const result = { ok: false, error: err };
    await setLastResult(result);
    await setSubsystem('capture', err.kind === 'invalid_token' ? 'warn' : 'ok');
    return result;
  }
}

/**
 * Project the server response into the lighter shape we cache for History.
 */
function toRecentRow(response) {
  return {
    capture_id: response.capture_id,
    doc_id: response.doc_id,
    web_url: response.web_url,
    status: response.status,
    platform: response.platform,
    topic_path: response.initial_path,
    created_at: response.created_at,
  };
}
```

- [ ] **Step 3: Run tests — confirm no regression**

```sh
cd browser-extension && npm test
```

Expected: still 24 / 24 (18 prior + 6 new payload tests from Task 2.1).

- [ ] **Step 4: Commit**

```sh
git add browser-extension/background.js
git commit -m "feat(extension): background handles capture messages (phase 2.2)"
```

---

## Task 3: Popup rebuild (HTML + CSS + JS)

**Files:**
- Modify: `browser-extension/popup/popup.html`
- Create: `browser-extension/popup/popup.css`
- Modify: `browser-extension/popup/popup.js`

The v0.1 popup has all CSS inline in a `<style>` block. We're extracting CSS into `popup.css` and rebuilding the HTML/JS for the new capture flow.

- [ ] **Step 1: Replace `popup/popup.html` with the new layout**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>AFFiNE Capture</title>
  <link rel="stylesheet" href="../lib/design-tokens.css">
  <link rel="stylesheet" href="popup.css">
</head>
<body>
  <header class="page-header">
    <img id="favicon" alt="" width="16" height="16">
    <span id="pageTitle" class="page-title">Loading…</span>
  </header>

  <button id="captureBtn" class="primary" type="button">Save to AFFiNE</button>

  <div id="captureStatus" class="capture-status" hidden></div>

  <footer class="footer">
    <div id="cookieStatus" class="cookie-status">Cookies: …</div>
    <a id="openOptions" class="link" href="#">Open AFFiNE Capture →</a>
  </footer>

  <script type="module" src="popup.js"></script>
</body>
</html>
```

- [ ] **Step 2: Create `popup/popup.css`**

```css
/* AFFiNE Capture — popup styles (share-sheet style; spec §4.1) */

body {
  font-family: var(--af-font);
  width: 360px;
  margin: 0;
  padding: var(--af-space-3);
  color: var(--af-navy);
  background: var(--af-surface);
  font: var(--af-body);
}

.page-header {
  display: flex;
  align-items: center;
  gap: var(--af-space-2);
  margin-bottom: var(--af-space-3);
  min-height: 20px;
}

.page-title {
  font: var(--af-small);
  color: var(--af-text-body);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1;
}

button.primary {
  width: 100%;
  padding: var(--af-space-2) var(--af-space-3);
  font: var(--af-body);
  font-weight: 600;
  color: var(--af-surface);
  background: var(--af-blue);
  border: none;
  border-radius: var(--af-radius-button);
  cursor: pointer;
  transition: transform .15s ease, filter .15s ease;
}
button.primary:hover { transform: translateY(-1px); }
button.primary:active { filter: brightness(0.92); transform: none; }
button.primary:disabled { opacity: .5; cursor: default; transform: none; }

.capture-status {
  margin-top: var(--af-space-3);
  padding: var(--af-space-2) var(--af-space-3);
  font: var(--af-small);
  border-radius: var(--af-radius-button);
  background: var(--af-bg-soft);
  color: var(--af-navy);
}
.capture-status.ok    { background: #E7F5E7; color: #1A6E1A; }
.capture-status.err   { background: #FDE7E7; color: #A02020; }
.capture-status .open-link {
  color: inherit;
  text-decoration: underline;
  margin-left: var(--af-space-2);
}

.footer {
  margin-top: var(--af-space-3);
  padding-top: var(--af-space-2);
  border-top: 1px solid var(--af-border);
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--af-space-2);
  font: var(--af-small);
  color: var(--af-text-body);
}
.footer .link {
  color: var(--af-blue);
  text-decoration: none;
}
.footer .link:hover { text-decoration: underline; }

.cookie-status.warn { color: #856404; }
.cookie-status.err  { color: var(--af-error); }
```

- [ ] **Step 3: Replace `popup/popup.js`**

```js
/**
 * AFFiNE Capture — toolbar popup.
 *
 * Mirrors the iOS Share Extension UI (spec §4.1):
 *   - shows current page (favicon + title)
 *   - primary "Save to AFFiNE" button
 *   - status row swaps in place: idle → capturing → saved (with web_url) | error
 *   - footer with cookie subsystem status + deep-link to options
 *
 * Auto-closes 2s after a successful capture (preserves v0.1 cookie sync UX).
 */
import { buildPayloadFromTab } from '../capture/payload.js';

const $favicon       = document.getElementById('favicon');
const $pageTitle     = document.getElementById('pageTitle');
const $captureBtn    = document.getElementById('captureBtn');
const $captureStatus = document.getElementById('captureStatus');
const $cookieStatus  = document.getElementById('cookieStatus');
const $openOptions   = document.getElementById('openOptions');

renderHeader();
renderCookieStatus();

$captureBtn.addEventListener('click', async () => {
  $captureBtn.disabled = true;
  setCaptureStatus('Capturing…', null);
  const tab = await getActiveTab();
  if (!tab) { $captureBtn.disabled = false; setCaptureStatus('No tab to capture', 'err'); return; }
  const payload = buildPayloadFromTab(tab);
  let result;
  try {
    result = await chrome.runtime.sendMessage({ type: 'capture', payload });
  } catch (e) {
    setCaptureStatus(`Failed: ${e?.message ?? e}`, 'err');
    $captureBtn.disabled = false;
    return;
  }
  if (result?.ok) {
    setCaptureStatusSaved(result.web_url);
    setTimeout(() => window.close(), 2000);
  } else {
    setCaptureStatusError(result?.error);
    $captureBtn.disabled = false;
  }
});

$openOptions.addEventListener('click', e => {
  e.preventDefault();
  if (chrome.runtime.openOptionsPage) chrome.runtime.openOptionsPage();
  else window.open(chrome.runtime.getURL('options/options.html'));
});

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab ?? null;
}

async function renderHeader() {
  const tab = await getActiveTab();
  if (!tab) { $pageTitle.textContent = 'No active tab'; return; }
  $pageTitle.textContent = tab.title || tab.url || 'Untitled';
  if (tab.favIconUrl) $favicon.src = tab.favIconUrl;
}

async function renderCookieStatus() {
  const lastSync = await chrome.runtime.sendMessage({ type: 'get-last-sync' });
  if (!lastSync) { $cookieStatus.textContent = 'Cookies: never synced'; return; }
  if (!lastSync.ok) {
    $cookieStatus.textContent = `Cookies: ${lastSync.error ?? 'failed'}`;
    $cookieStatus.classList.add('err');
    return;
  }
  const ago = formatRelative(new Date(lastSync.synced_at));
  $cookieStatus.textContent = `Cookies: synced ${ago}`;
  if (lastSync.verdict === 'stale') $cookieStatus.classList.add('warn');
  if (lastSync.verdict === 'missing') $cookieStatus.classList.add('err');
}

function setCaptureStatus(text, kind) {
  $captureStatus.hidden = false;
  $captureStatus.className = 'capture-status' + (kind ? ` ${kind}` : '');
  $captureStatus.textContent = text;
}

function setCaptureStatusSaved(webUrl) {
  $captureStatus.hidden = false;
  $captureStatus.className = 'capture-status ok';
  $captureStatus.textContent = '✓ Saved';
  if (webUrl) {
    const a = document.createElement('a');
    a.href = webUrl;
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = 'Open in AFFiNE ↗';
    a.className = 'open-link';
    $captureStatus.append(' ', a);
  }
}

function setCaptureStatusError(err) {
  $captureStatus.hidden = false;
  $captureStatus.className = 'capture-status err';
  if (err?.kind === 'invalid_token') {
    $captureStatus.textContent = 'Token rejected — open Settings';
  } else if (err?.kind === 'config') {
    $captureStatus.textContent = 'Not configured — open Settings';
  } else if (err?.kind === 'rate_limited') {
    $captureStatus.textContent = `Rate limited; try again in ${err.retryAfter ?? '?'}s`;
  } else if (err?.kind === 'network') {
    $captureStatus.textContent = `Couldn't reach ingest`;
  } else {
    $captureStatus.textContent = err?.message ?? 'Failed';
  }
}

function formatRelative(date) {
  const sec = Math.floor((Date.now() - date.getTime()) / 1000);
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}
```

- [ ] **Step 4: Tests pass + manual visual check**

```sh
npm test
```

Expected: still 24 / 24.

- [ ] **Step 5: Commit**

```sh
git add browser-extension/popup/
git commit -m "feat(extension): popup capture flow + design-system styling (phase 2.3)"
```

---

## Task 4: Manual smoke (USER)

After Tasks 1–3 land, the user reloads the extension and runs the spec-§8 popup acceptance:

- [ ] Open popup on any tab → favicon + truncated title visible.
- [ ] Click "Save to AFFiNE" → status flips to "Capturing…" then "✓ Saved · Open in AFFiNE ↗" within 2s.
- [ ] Click "Open in AFFiNE ↗" → opens `web_url` in new tab.
- [ ] Popup auto-closes 2s after success.
- [ ] Click again on the same page → server idempotency: same `capture_id` returned, no duplicate doc.
- [ ] Server-side: `GET /captures?limit=1` shows the new row with `source_app: <hostname>`.
- [ ] Cookie subsystem regression: footer shows current sync status; "Open AFFiNE Capture" link works.
- [ ] Error path: temporarily set a bogus token in Settings → next capture → status flips red "Token rejected — open Settings". Toolbar badge shows red `!`. Restore token → next capture clears.

Report any failures back here for fix subagents before Phase 3.
