# Phase 1: Browser extension refactor (cookies/* + lib/* + manifest)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize `portainer-stack/browser-extension/` from the v0.1 cookie-only layout (single `background.js`, root-level `popup.html` / `options.html`) into the v0.2 multitool layout (`lib/`, `cookies/`, `popup/`, `options/` subfolders) and rename to **AFFiNE Capture**. **No user-visible behavior change** — the v0.1 cookie sync must continue to work byte-for-byte after this phase.

**Spec:** [`docs/specs/2026-05-10-browser-extension-multitool-design.md`](../specs/2026-05-10-browser-extension-multitool-design.md) §2, §3, §5

**Macro plan:** [`docs/plans/2026-05-10-browser-extension-multitool-macro-plan.md`](2026-05-10-browser-extension-multitool-macro-plan.md) Phase 1

**Architecture:** Three new things land — a shared `lib/` core (api/storage/badge/design-tokens), a `cookies/` module that owns today's behavior, and a renamed/expanded manifest. Plus four organizational moves: `popup.{html,js}` → `popup/`, `options.{html,js}` → `options/`. Every existing v0.1 line of cookie logic survives, just relocated, with `fetch` calls re-routed through `lib/api.js`.

**Tech Stack:**
- Vanilla JS ES2022 modules
- vitest (test runner — same as `affine-mcp-agent` in this repo)
- jsdom (DOM in tests where needed)
- A small `chrome` API stub object for tests (no dependency required)

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `browser-extension/package.json` | Create | NEW — vitest devDep, `npm test` script |
| `browser-extension/vitest.config.js` | Create | NEW — test glob `**/__tests__/**/*.test.js` |
| `browser-extension/lib/design-tokens.css` | Create | AFFiNE design tokens (CSS custom properties) |
| `browser-extension/lib/storage.js` | Create | Typed chrome.storage.local helpers |
| `browser-extension/lib/api.js` | Create | Shared HTTP client w/ Bearer auth + typed errors |
| `browser-extension/lib/badge.js` | Create | Toolbar badge state — single source of truth |
| `browser-extension/lib/__tests__/storage.test.js` | Create | Tests for storage helpers |
| `browser-extension/lib/__tests__/api.test.js` | Create | Tests for HTTP client + error mapping |
| `browser-extension/cookies/netscape.js` | Create | Netscape cookies.txt format helper (moved out of `background.js`) |
| `browser-extension/cookies/sync.js` | Create | Cookie sync flow (moved out of `background.js`); now uses `lib/api.js` |
| `browser-extension/cookies/__tests__/netscape.test.js` | Create | Tests for `cookiesToNetscape` |
| `browser-extension/background.js` | Modify | Slim to a router that imports `cookies/sync.js` and registers listeners |
| `browser-extension/manifest.json` | Modify | Rename to "AFFiNE Capture", v0.2.0, add new perms + host_permissions, update `popup` and `options_ui` paths |
| `browser-extension/popup.html` | Move → `browser-extension/popup/popup.html` | (Move) |
| `browser-extension/popup.js` | Move → `browser-extension/popup/popup.js` | (Move) |
| `browser-extension/options.html` | Move → `browser-extension/options/options.html` | (Move) |
| `browser-extension/options.js` | Move → `browser-extension/options/options.js` | (Move) |
| `browser-extension/README.md` | Modify | Reflect "AFFiNE Capture v0.2" identity (preserve all cookie content; flag multitool intent for upcoming phases) |

---

## Task 1: Test harness + `lib/design-tokens.css`

**Files:**
- Create: `browser-extension/package.json`
- Create: `browser-extension/vitest.config.js`
- Create: `browser-extension/.gitignore` (just `node_modules/`)
- Create: `browser-extension/lib/design-tokens.css`

- [ ] **Step 1: package.json**

```json
{
  "name": "affine-capture-extension",
  "version": "0.2.0",
  "private": true,
  "type": "module",
  "scripts": {
    "test": "vitest run",
    "test:watch": "vitest"
  },
  "devDependencies": {
    "vitest": "^2.1.0",
    "jsdom": "^25.0.0"
  }
}
```

- [ ] **Step 2: vitest.config.js**

```js
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['**/__tests__/**/*.test.js'],
    environment: 'node',          // override per-file via /** @vitest-environment jsdom */
    globals: false,
    setupFiles: ['./test/setup.js'],
  },
});
```

- [ ] **Step 3: test/setup.js — minimal `chrome` stub**

```js
// Per-test files override individual chrome.* methods with vi.fn()s. This file
// just gives `globalThis.chrome` a baseline shape so importing modules don't
// throw on top-level `chrome.foo` accesses.
import { vi } from 'vitest';

globalThis.chrome = {
  storage: {
    local: {
      get: vi.fn(async () => ({})),
      set: vi.fn(async () => {}),
    },
  },
  cookies: {
    getAll: vi.fn(async () => []),
    onChanged: { addListener: vi.fn() },
  },
  alarms: {
    create: vi.fn(),
    onAlarm: { addListener: vi.fn() },
    getAll: vi.fn(async () => []),
  },
  runtime: {
    onInstalled: { addListener: vi.fn() },
    onMessage: { addListener: vi.fn() },
    onStartup: { addListener: vi.fn() },
  },
  action: {
    setBadgeText: vi.fn(async () => {}),
    setBadgeBackgroundColor: vi.fn(async () => {}),
  },
  permissions: {
    contains: vi.fn(async () => false),
    request: vi.fn(async () => true),
    remove: vi.fn(async () => true),
  },
};
```

- [ ] **Step 4: Install + smoke**

Run:

```sh
cd portainer-stack/browser-extension
npm install
npm test
```

Expected: vitest reports "No test files found" and exits 0.

- [ ] **Step 5: lib/design-tokens.css**

Full token file per spec §5. Drop in:

```css
/*
 * AFFiNE Capture — design tokens.
 * Source: portainer-stack/docs/specs/2026-05-10-browser-extension-multitool-design.md §5
 *
 * Imported by every UI surface (popup, options, Web Component Shadow DOMs).
 */
:root {
  /* Color */
  --af-blue:     #2B85FF;
  --af-navy:     #001A3F;
  --af-success:  #4CAF50;
  --af-error:    #FF4D4F;
  --af-bg-soft:  #F5F9FF;
  --af-surface:  #FFFFFF;
  --af-gray-50:  #F8F9FA;
  --af-border:   #E5E7EB;
  --af-text-body: #4B5563;

  /* Radii */
  --af-radius-button: 8px;
  --af-radius-card:   12px;
  --af-radius-pill:   999px;

  /* Spacing — 8px base */
  --af-space-1: 4px;
  --af-space-2: 8px;
  --af-space-3: 16px;
  --af-space-4: 24px;
  --af-space-5: 32px;
  --af-space-6: 64px;

  /* Typography */
  --af-font: 'Inter', -apple-system, system-ui, 'Segoe UI', sans-serif;
  --af-font-mono: 'Roboto Mono', 'JetBrains Mono', ui-monospace, monospace;

  /* Type scale */
  --af-h1: 700 48px/1.1 var(--af-font);
  --af-h2: 600 36px/1.2 var(--af-font);
  --af-h3: 600 24px/1.3 var(--af-font);
  --af-body-l: 400 18px/1.5 var(--af-font);
  --af-body:   400 16px/1.5 var(--af-font);
  --af-small:  500 14px/1.4 var(--af-font);

  /* Elevation */
  --af-shadow-card: 0 1px 2px rgba(0, 26, 63, 0.06), 0 4px 12px rgba(0, 26, 63, 0.04);
}
```

- [ ] **Step 6: Commit**

```sh
git add browser-extension/package.json browser-extension/vitest.config.js \
        browser-extension/test/setup.js browser-extension/.gitignore \
        browser-extension/lib/design-tokens.css
git commit -m "feat(extension): vitest harness + design tokens (phase 1.1)"
```

---

## Task 2: `lib/storage.js` + tests

**Files:**
- Create: `browser-extension/lib/storage.js`
- Create: `browser-extension/lib/__tests__/storage.test.js`

- [ ] **Step 1: Failing test**

`lib/__tests__/storage.test.js`:

```js
/** @vitest-environment node */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { getConfig, setConfig, getLastSync, setLastSync,
         getLastResult, setLastResult, getRecentCaptures, setRecentCaptures }
  from '../storage.js';

describe('lib/storage', () => {
  beforeEach(() => {
    chrome.storage.local.get = vi.fn(async (keys) => ({}));
    chrome.storage.local.set = vi.fn(async () => {});
  });

  it('getConfig returns ingestUrl + ingestToken + extendedScope from local storage', async () => {
    chrome.storage.local.get = vi.fn(async () => ({
      ingestUrl: 'https://example.com',
      ingestToken: 'tok',
      extendedScope: true,
    }));
    expect(await getConfig()).toEqual({
      ingestUrl: 'https://example.com', ingestToken: 'tok', extendedScope: true,
    });
  });

  it('setConfig writes only provided keys (partial update)', async () => {
    await setConfig({ ingestUrl: 'https://x' });
    expect(chrome.storage.local.set).toHaveBeenCalledWith({ ingestUrl: 'https://x' });
  });

  it('getLastSync returns null if never synced', async () => {
    expect(await getLastSync()).toBeNull();
  });

  it('setRecentCaptures truncates to 50 entries', async () => {
    const items = Array.from({length: 75}, (_, i) => ({capture_id: String(i)}));
    await setRecentCaptures(items);
    const passed = chrome.storage.local.set.mock.calls[0][0].recentCaptures;
    expect(passed).toHaveLength(50);
    expect(passed[0].capture_id).toBe('0');  // keep newest first; caller sorts
  });

  it('getRecentCaptures returns [] if missing', async () => {
    expect(await getRecentCaptures()).toEqual([]);
  });

  it('getLastResult returns null if never set', async () => {
    expect(await getLastResult()).toBeNull();
  });
});
```

- [ ] **Step 2: Run test — should fail**

```sh
npm test -- lib/__tests__/storage.test.js
```

Expected: FAIL with "Cannot find module './storage.js'".

- [ ] **Step 3: Implement `lib/storage.js`**

```js
/**
 * Typed chrome.storage.local helpers. The only file in the extension that
 * directly reads/writes storage keys; everything else routes through here so
 * key naming stays consistent.
 *
 * Schema (see spec §6.3):
 *   ingestUrl       - string
 *   ingestToken     - string
 *   extendedScope   - bool
 *   lastSync        - object  (cookie subsystem result)
 *   lastResult      - object  (last capture result)
 *   recentCaptures  - array (<= 50 capture rows for instant History render)
 */

const RECENT_MAX = 50;

export async function getConfig() {
  const { ingestUrl, ingestToken, extendedScope } =
    await chrome.storage.local.get(['ingestUrl', 'ingestToken', 'extendedScope']);
  return {
    ingestUrl: ingestUrl ?? null,
    ingestToken: ingestToken ?? null,
    extendedScope: !!extendedScope,
  };
}

export async function setConfig(patch) {
  await chrome.storage.local.set(patch);
}

export async function getLastSync() {
  const { lastSync } = await chrome.storage.local.get('lastSync');
  return lastSync ?? null;
}

export async function setLastSync(value) {
  await chrome.storage.local.set({ lastSync: value });
}

export async function getLastResult() {
  const { lastResult } = await chrome.storage.local.get('lastResult');
  return lastResult ?? null;
}

export async function setLastResult(value) {
  await chrome.storage.local.set({ lastResult: value });
}

export async function getRecentCaptures() {
  const { recentCaptures } = await chrome.storage.local.get('recentCaptures');
  return recentCaptures ?? [];
}

export async function setRecentCaptures(items) {
  const capped = (items ?? []).slice(0, RECENT_MAX);
  await chrome.storage.local.set({ recentCaptures: capped });
}
```

- [ ] **Step 4: Run test — should pass**

```sh
npm test -- lib/__tests__/storage.test.js
```

Expected: 6 / 6 passing.

- [ ] **Step 5: Commit**

```sh
git add browser-extension/lib/storage.js browser-extension/lib/__tests__/storage.test.js
git commit -m "feat(extension): lib/storage typed helpers (phase 1.2)"
```

---

## Task 3: `lib/api.js` + tests

**Files:**
- Create: `browser-extension/lib/api.js`
- Create: `browser-extension/lib/__tests__/api.test.js`

- [ ] **Step 1: Failing test**

`lib/__tests__/api.test.js`:

```js
/** @vitest-environment node */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { request, IngestError } from '../api.js';
import * as storage from '../storage.js';

describe('lib/api.request', () => {
  beforeEach(() => {
    vi.spyOn(storage, 'getConfig').mockResolvedValue({
      ingestUrl: 'https://ingest.test',
      ingestToken: 'tok',
      extendedScope: false,
    });
    globalThis.fetch = vi.fn();
  });

  it('attaches Bearer auth and JSON content-type', async () => {
    fetch.mockResolvedValue(new Response(JSON.stringify({ok: true}),
      {status: 200, headers: {'content-type': 'application/json'}}));
    await request('GET', '/health');
    const [url, opts] = fetch.mock.calls[0];
    expect(url).toBe('https://ingest.test/health');
    expect(opts.headers.Authorization).toBe('Bearer tok');
  });

  it('strips trailing slash on ingestUrl', async () => {
    storage.getConfig.mockResolvedValue({
      ingestUrl: 'https://ingest.test/', ingestToken: 't', extendedScope: false,
    });
    fetch.mockResolvedValue(new Response('{}', {status: 200,
      headers: {'content-type': 'application/json'}}));
    await request('GET', '/health');
    expect(fetch.mock.calls[0][0]).toBe('https://ingest.test/health');
  });

  it('serializes JSON body', async () => {
    fetch.mockResolvedValue(new Response('{}', {status: 200,
      headers: {'content-type': 'application/json'}}));
    await request('POST', '/capture', { body: { url: 'https://x' } });
    const opts = fetch.mock.calls[0][1];
    expect(opts.headers['Content-Type']).toBe('application/json');
    expect(opts.body).toBe('{"url":"https://x"}');
  });

  it('passes raw body through when bodyType=text', async () => {
    fetch.mockResolvedValue(new Response('{}', {status: 200,
      headers: {'content-type': 'application/json'}}));
    await request('POST', '/youtube/cookies', { body: 'cookie\ttext', bodyType: 'text' });
    const opts = fetch.mock.calls[0][1];
    expect(opts.headers['Content-Type']).toBe('text/plain');
    expect(opts.body).toBe('cookie\ttext');
  });

  it('throws IngestError invalid_token on 401', async () => {
    fetch.mockResolvedValue(new Response(JSON.stringify({error: {code: 'INVALID_TOKEN'}}),
      {status: 401, headers: {'content-type': 'application/json'}}));
    await expect(request('GET', '/health')).rejects.toMatchObject({
      kind: 'invalid_token', status: 401,
    });
  });

  it('throws IngestError rate_limited on 429 with retryAfter', async () => {
    fetch.mockResolvedValue(new Response('{}', {
      status: 429, headers: {'content-type': 'application/json', 'retry-after': '30'},
    }));
    await expect(request('GET', '/health')).rejects.toMatchObject({
      kind: 'rate_limited', retryAfter: 30,
    });
  });

  it('throws IngestError server on 500', async () => {
    fetch.mockResolvedValue(new Response('{}', {status: 500,
      headers: {'content-type': 'application/json'}}));
    await expect(request('GET', '/health')).rejects.toMatchObject({ kind: 'server' });
  });

  it('throws IngestError network on fetch reject', async () => {
    fetch.mockRejectedValue(new TypeError('Failed to fetch'));
    await expect(request('GET', '/health')).rejects.toMatchObject({ kind: 'network' });
  });

  it('throws IngestError config when ingestUrl unset', async () => {
    storage.getConfig.mockResolvedValue({ingestUrl: null, ingestToken: null, extendedScope: false});
    await expect(request('GET', '/health')).rejects.toMatchObject({ kind: 'config' });
  });
});
```

- [ ] **Step 2: Run test — fails**

```sh
npm test -- lib/__tests__/api.test.js
```

Expected: FAIL — "Cannot find module './api.js'".

- [ ] **Step 3: Implement `lib/api.js`**

```js
/**
 * Single shared HTTP client for the ingest service. Used by:
 *   - cookies/sync.js  (POST /youtube/cookies, GET /youtube/cookies/status)
 *   - capture/client.js (POST /capture, GET /captures, etc. — added in Phase 2)
 *
 * Returns parsed JSON on 2xx; throws an IngestError on every non-2xx and on
 * network failure. The caller never sees a Response object.
 *
 * Errors are typed by `kind` so UI surfaces can map them consistently
 * (see spec §7).
 */
import { getConfig } from './storage.js';

const TIMEOUT_MS = 10_000;

export class IngestError extends Error {
  constructor({ kind, message, status, retryAfter }) {
    super(message ?? kind);
    this.kind = kind;
    this.status = status ?? null;
    this.retryAfter = retryAfter ?? null;
  }
}

/**
 * @param {'GET'|'POST'|'DELETE'} method
 * @param {string} path  — server-relative, e.g. "/capture"
 * @param {{body?: any, bodyType?: 'json'|'text', signal?: AbortSignal}} [opts]
 * @returns {Promise<any>}  — parsed JSON, or `null` for empty 204s
 */
export async function request(method, path, opts = {}) {
  const { ingestUrl, ingestToken } = await getConfig();
  if (!ingestUrl || !ingestToken) {
    throw new IngestError({ kind: 'config', message: 'Server URL / token not configured' });
  }

  const url = `${ingestUrl.replace(/\/$/, '')}${path}`;
  const headers = { 'Authorization': `Bearer ${ingestToken}` };
  let body;
  if (opts.body !== undefined) {
    if (opts.bodyType === 'text') {
      headers['Content-Type'] = 'text/plain';
      body = opts.body;
    } else {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(opts.body);
    }
  }

  let resp;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    resp = await fetch(url, {
      method, headers, body,
      signal: opts.signal ?? controller.signal,
    });
  } catch (e) {
    throw new IngestError({ kind: 'network', message: e?.message ?? String(e) });
  } finally {
    clearTimeout(timer);
  }

  if (resp.status === 401) {
    throw new IngestError({ kind: 'invalid_token', status: 401, message: 'Token rejected' });
  }
  if (resp.status === 429) {
    const retryAfter = Number(resp.headers.get('retry-after')) || null;
    throw new IngestError({ kind: 'rate_limited', status: 429, retryAfter,
      message: 'Rate limited' });
  }
  if (resp.status >= 500) {
    throw new IngestError({ kind: 'server', status: resp.status,
      message: `Server error ${resp.status}` });
  }
  if (!resp.ok) {
    throw new IngestError({ kind: 'server', status: resp.status,
      message: `HTTP ${resp.status}` });
  }
  if (resp.status === 204) return null;
  const ct = resp.headers.get('content-type') ?? '';
  if (ct.includes('application/json')) return await resp.json();
  return await resp.text();
}
```

- [ ] **Step 4: Run test — passes**

```sh
npm test -- lib/__tests__/api.test.js
```

Expected: 9 / 9 passing.

- [ ] **Step 5: Commit**

```sh
git add browser-extension/lib/api.js browser-extension/lib/__tests__/api.test.js
git commit -m "feat(extension): lib/api shared HTTP client + typed errors (phase 1.3)"
```

---

## Task 4: `lib/badge.js`

**Files:**
- Create: `browser-extension/lib/badge.js`

(No tests — the file is a pure dispatcher over the chrome.action API. Behavior is verified manually in the smoke test.)

- [ ] **Step 1: Implement `lib/badge.js`**

```js
/**
 * Single source of truth for the toolbar badge.
 *
 * The badge can show a "!" warning from EITHER subsystem:
 *   - cookies-stale  (server cookies file old / missing)
 *   - capture-failed (last capture errored, e.g. token rejected)
 *
 * v0.2 collapses both into a single "!" — the popup explains which when
 * opened. State is persisted to chrome.storage.local so it survives the
 * service worker dying and restarting.
 */

const COLOR_WARN = '#d33a2c';   // matches v0.1 BADGE_COLOR_STALE
const TEXT_WARN = '!';

/**
 * Update badge state for one subsystem and recompute the visible badge.
 *
 * @param {'cookies' | 'capture'} subsystem
 * @param {'ok' | 'warn' | 'unknown'} state
 */
export async function setSubsystem(subsystem, state) {
  const { badgeState } = await chrome.storage.local.get('badgeState');
  const next = { ...(badgeState ?? {}), [subsystem]: state };
  await chrome.storage.local.set({ badgeState: next });
  await applyBadge(next);
}

/**
 * Re-apply the badge from current state (used at service-worker startup).
 */
export async function refreshBadge() {
  const { badgeState } = await chrome.storage.local.get('badgeState');
  await applyBadge(badgeState ?? {});
}

async function applyBadge(state) {
  const anyWarn = Object.values(state).some(s => s === 'warn');
  await chrome.action.setBadgeText({ text: anyWarn ? TEXT_WARN : '' });
  if (anyWarn) {
    await chrome.action.setBadgeBackgroundColor({ color: COLOR_WARN });
  }
}
```

- [ ] **Step 2: Commit**

```sh
git add browser-extension/lib/badge.js
git commit -m "feat(extension): lib/badge unified toolbar badge state (phase 1.4)"
```

---

## Task 5: `cookies/netscape.js` + `cookies/sync.js` (extracted)

**Files:**
- Create: `browser-extension/cookies/netscape.js`
- Create: `browser-extension/cookies/sync.js`
- Create: `browser-extension/cookies/__tests__/netscape.test.js`

- [ ] **Step 1: Failing test for `cookiesToNetscape`**

`cookies/__tests__/netscape.test.js`:

```js
/** @vitest-environment node */
import { describe, it, expect } from 'vitest';
import { cookiesToNetscape } from '../netscape.js';

describe('cookies/netscape.cookiesToNetscape', () => {
  it('emits header + 7 tab-separated columns per cookie', () => {
    const out = cookiesToNetscape([{
      domain: '.youtube.com', path: '/', secure: true, session: false,
      expirationDate: 1700000000, name: 'SID', value: 'abc',
    }]);
    expect(out).toMatch(/^# Netscape HTTP Cookie File/);
    const dataLines = out.trim().split('\n').filter(l => !l.startsWith('#') && l);
    expect(dataLines).toHaveLength(1);
    expect(dataLines[0].split('\t')).toEqual([
      '.youtube.com', 'TRUE', '/', 'TRUE', '1700000000', 'SID', 'abc',
    ]);
  });

  it('uses "0" for session cookies', () => {
    const out = cookiesToNetscape([{
      domain: 'youtube.com', path: '/', secure: false, session: true,
      name: 'tmp', value: 'v',
    }]);
    expect(out).toMatch(/\t0\t/);
  });

  it('marks subdomain inclusion based on leading dot', () => {
    const out = cookiesToNetscape([
      { domain: '.youtube.com', path: '/', secure: false, session: true, name: 'a', value: '1' },
      { domain: 'youtube.com',  path: '/', secure: false, session: true, name: 'b', value: '2' },
    ]);
    const lines = out.trim().split('\n').filter(l => !l.startsWith('#') && l);
    expect(lines[0]).toContain('TRUE');
    expect(lines[1]).toMatch(/^youtube\.com\tFALSE/);
  });
});
```

- [ ] **Step 2: Run — fails**

```sh
npm test -- cookies/__tests__/netscape.test.js
```

Expected: FAIL — "Cannot find module".

- [ ] **Step 3: Implement `cookies/netscape.js`**

Copy the v0.1 implementation from `background.js` lines 82–104 verbatim, exported as a named export.

```js
/**
 * Convert chrome.cookies.Cookie[] to Netscape cookies.txt format.
 * Tab-separated 7 fields per row:
 *   domain  include_subdomains  path  secure  expires  name  value
 *
 * `expires=0` for session cookies (yt-dlp + transcript-api both tolerate).
 * Values are emitted as-is (chrome.cookies API returns them URL-decoded
 * already; yt-dlp doesn't expect re-encoding).
 */
export function cookiesToNetscape(cookies) {
  const header = [
    '# Netscape HTTP Cookie File',
    '# Generated by AFFiNE Capture extension',
    '# Do not edit manually — overwritten on next sync',
    '',
  ];
  const rows = cookies.map(c => {
    const includeSubdomains = c.domain.startsWith('.') ? 'TRUE' : 'FALSE';
    const secure = c.secure ? 'TRUE' : 'FALSE';
    const expires = c.session ? '0' : Math.floor(c.expirationDate || 0);
    return [
      c.domain, includeSubdomains, c.path, secure, String(expires), c.name, c.value,
    ].join('\t');
  });
  return header.concat(rows).join('\n') + '\n';
}
```

- [ ] **Step 4: Run — passes**

```sh
npm test -- cookies/__tests__/netscape.test.js
```

Expected: 3 / 3 passing.

- [ ] **Step 5: Implement `cookies/sync.js`**

Move from `background.js`:
- `collectYouTubeCookies` → goes here
- `fetchServerStatus` → goes here BUT use `lib/api.js` for the actual request
- `verdictFromStatus` → goes here
- `syncCookies` → goes here BUT use `lib/api.js` for the POST AND `lib/storage.js` for `getConfig`/`setLastSync` AND `lib/badge.js` for badge updates

```js
/**
 * Cookie sync subsystem — moved out of background.js for the v0.2 multitool
 * refactor. Behavior is unchanged from v0.1; only the imports and HTTP plumbing
 * have been swapped to the shared lib/* modules.
 *
 * See spec §6.2 (data flow) and the v0.1 README at portainer-stack/browser-extension/README.md.
 */
import { request, IngestError } from '../lib/api.js';
import { getConfig, getLastSync, setLastSync } from '../lib/storage.js';
import { setSubsystem } from '../lib/badge.js';
import { cookiesToNetscape } from './netscape.js';

const STALE_AFTER_SECONDS = 60 * 60 * 24;  // 24h — beyond this, "warn".

export async function collectYouTubeCookies() {
  const requests = [
    chrome.cookies.getAll({ domain: 'youtube.com' }),
    chrome.cookies.getAll({ domain: '.youtube.com' }),
  ];

  const { extendedScope } = await getConfig();
  if (extendedScope) {
    const hasPerm = await chrome.permissions.contains({
      origins: ['*://accounts.google.com/*'],
    });
    if (hasPerm) {
      requests.push(chrome.cookies.getAll({ domain: 'accounts.google.com' }));
      requests.push(chrome.cookies.getAll({ domain: '.google.com' }));
    }
  }

  const buckets = await Promise.all(requests);
  const seen = new Set();
  const out = [];
  for (const c of buckets.flat()) {
    const key = `${c.name}|${c.domain}|${c.path}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(c);
  }
  return out;
}

export async function fetchServerStatus() {
  try {
    return await request('GET', '/youtube/cookies/status');
  } catch (e) {
    return null;  // unknown — don't change badge
  }
}

export function verdictFromStatus(status) {
  if (!status) return 'unknown';
  if (!status.exists) return 'missing';
  if ((status.age_seconds ?? 0) >= STALE_AFTER_SECONDS) return 'stale';
  return 'fresh';
}

/**
 * Full sync flow. Persists `lastSync` so the popup can render without
 * re-running the work, and updates the cookie subsystem badge.
 */
export async function syncCookies() {
  const { ingestUrl, ingestToken } = await getConfig();
  if (!ingestUrl || !ingestToken) {
    const result = { ok: false, error: 'not configured', synced_at: null };
    await setLastSync(result);
    return result;
  }

  let cookies;
  try {
    cookies = await collectYouTubeCookies();
  } catch (e) {
    const result = { ok: false, error: `collect failed: ${e?.message ?? e}`, synced_at: null };
    await setLastSync(result);
    return result;
  }

  if (cookies.length === 0) {
    const result = { ok: false, error: 'no YouTube cookies — log into YouTube first', synced_at: null };
    await setLastSync(result);
    return result;
  }

  const body = cookiesToNetscape(cookies);
  let uploadOk = true;
  let uploadError = null;
  try {
    await request('POST', '/youtube/cookies', { body, bodyType: 'text' });
  } catch (e) {
    uploadOk = false;
    uploadError = e instanceof IngestError ? e.message : String(e);
  }

  const serverStatus = uploadOk ? await fetchServerStatus() : null;
  const verdict = verdictFromStatus(serverStatus);

  const result = {
    ok: uploadOk,
    cookie_count: cookies.length,
    byte_count: body.length,
    synced_at: new Date().toISOString(),
    error: uploadError,
    server_status: serverStatus,
    verdict,
  };
  await setLastSync(result);
  await setSubsystem('cookies', verdict === 'stale' || verdict === 'missing' ? 'warn' : 'ok');
  return result;
}
```

- [ ] **Step 6: Commit**

```sh
git add browser-extension/cookies/
git commit -m "feat(extension): extract cookies/* from background.js (phase 1.5)"
```

---

## Task 6: Slim down `background.js` + update `manifest.json`

**Files:**
- Modify: `browser-extension/background.js`
- Modify: `browser-extension/manifest.json`

- [ ] **Step 1: Rewrite `background.js`**

Replace the entire current contents with this slim router:

```js
/**
 * AFFiNE Capture — background service worker.
 *
 * Top-level dispatcher only. Cookie sync logic lives in cookies/sync.js;
 * capture logic will be added in Phase 2 (capture/*). This file's job is to
 * register the chrome.* listeners and route them to the right module.
 */
import { syncCookies } from './cookies/sync.js';
import { refreshBadge } from './lib/badge.js';

const ALARM_DAILY_SYNC = 'yt-cookie-daily-sync';
const ALARM_DEBOUNCE_SYNC = 'yt-cookie-debounce-sync';
const DEBOUNCE_MINUTES = 0.5;

// First-install flow: kick off a sync (no-op if not configured) and create
// the daily safety-net alarm.
chrome.runtime.onInstalled.addListener(() => {
  syncCookies();
  chrome.alarms.create(ALARM_DAILY_SYNC, { periodInMinutes: 60 * 24 });
});

// Restore badge state from storage when the worker wakes up.
chrome.runtime.onStartup.addListener(() => {
  refreshBadge();
});

// Debounced sync on YouTube cookie changes.
chrome.cookies.onChanged.addListener(({ cookie }) => {
  if (!cookie?.domain?.includes('youtube.com')) return;
  chrome.alarms.create(ALARM_DEBOUNCE_SYNC, { delayInMinutes: DEBOUNCE_MINUTES });
});

chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === ALARM_DAILY_SYNC || alarm.name === ALARM_DEBOUNCE_SYNC) {
    syncCookies();
  }
});

// Manual triggers from popup / options.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === 'sync-now') {
    syncCookies().then(sendResponse);
    return true;
  }
  if (msg?.type === 'get-last-sync') {
    import('./lib/storage.js').then(m => m.getLastSync()).then(sendResponse);
    return true;
  }
});
```

- [ ] **Step 2: Update `manifest.json`**

```json
{
  "manifest_version": 3,
  "name": "AFFiNE Capture",
  "version": "0.2.0",
  "description": "Save any web content to your self-hosted AFFiNE workspace via the ingest service. Also keeps YouTube cookies synced so cobalt + yt-dlp can fetch authenticated content.",
  "permissions": [
    "cookies",
    "storage",
    "alarms",
    "contextMenus",
    "scripting",
    "activeTab",
    "notifications"
  ],
  "host_permissions": [
    "*://*.youtube.com/*",
    "*://*.instagram.com/*",
    "*://*.x.com/*",
    "*://*.twitter.com/*",
    "*://*.tiktok.com/*",
    "*://*.reddit.com/*",
    "https://*/*",
    "http://*/*"
  ],
  "optional_host_permissions": [
    "*://*.google.com/*",
    "*://accounts.google.com/*"
  ],
  "background": {
    "service_worker": "background.js",
    "type": "module"
  },
  "options_ui": {
    "page": "options/options.html",
    "open_in_tab": false
  },
  "action": {
    "default_popup": "popup/popup.html",
    "default_title": "AFFiNE Capture"
  },
  "icons": {
    "16": "icons/icon-16.png",
    "32": "icons/icon-32.png",
    "48": "icons/icon-48.png",
    "128": "icons/icon-128.png"
  },
  "browser_specific_settings": {
    "gecko": {
      "id": "affine-capture@affine.local",
      "strict_min_version": "109.0"
    }
  }
}
```

Notes:
- `name` and `description` reflect multitool scope.
- `permissions` adds the four new ones (`contextMenus`, `scripting`, `activeTab`, `notifications`) — used in Phases 2/3/8.
- `host_permissions` adds the five sites where content scripts will inject pills (Phase 8). `https://*/*` and `http://*/*` are kept so the user's ingest URL works at any host.
- `options_ui.page` and `action.default_popup` paths updated for the upcoming folder reorganization (Task 7).
- `browser_specific_settings.gecko.id` changed to `affine-capture@affine.local` (was `yt-cookie-sync@affine.local`) — Firefox uses this as the addon ID.

- [ ] **Step 3: Commit**

```sh
git add browser-extension/background.js browser-extension/manifest.json
git commit -m "feat(extension): slim background.js + AFFiNE Capture manifest v0.2 (phase 1.6)"
```

---

## Task 7: Move `popup.{html,js}` and `options.{html,js}` into folders

**Files:**
- Move: `browser-extension/popup.html` → `browser-extension/popup/popup.html`
- Move: `browser-extension/popup.js` → `browser-extension/popup/popup.js`
- Move: `browser-extension/options.html` → `browser-extension/options/options.html`
- Move: `browser-extension/options.js` → `browser-extension/options/options.js`

(`manifest.json` already references the new paths from Task 6.)

- [ ] **Step 1: Move popup files**

```sh
cd portainer-stack/browser-extension
mkdir -p popup options
git mv popup.html popup/popup.html
git mv popup.js popup/popup.js
git mv options.html options/options.html
git mv options.js options/options.js
```

- [ ] **Step 2: Verify popup.html script src is unchanged**

Open `popup/popup.html`; the `<script src="popup.js">` tag stays as-is — the script is in the same folder now.

- [ ] **Step 3: Verify options.html script src is unchanged**

Same — `<script src="options.js">` remains correct.

- [ ] **Step 4: Manual smoke check**

Reload the unpacked extension at `chrome://extensions/`. Confirm:
- Toolbar icon still opens the v0.1 popup.
- "Settings" button still opens the v0.1 options page.
- "Sync now" still works → server log shows the cookie upload.

- [ ] **Step 5: Commit**

```sh
git commit -m "refactor(extension): move popup/* and options/* into folders (phase 1.7)"
```

---

## Task 8: README rewrite + manual acceptance

**Files:**
- Modify: `browser-extension/README.md`

- [ ] **Step 1: Rewrite README header**

Open `browser-extension/README.md` and replace the title + opening section:

```markdown
# AFFiNE Capture — browser extension

A multitool extension for your self-hosted AFFiNE ingest service. Two
subsystems share one extension:

1. **Capture** — send any web content (page URL, link, selected text, image,
   per-post pills on YouTube/Instagram/X/TikTok/Reddit) to your AFFiNE
   workspace via `POST /capture`. *Coming in Phase 2 — popup capture today
   (v0.2.x dev).*
2. **Cookie sync** — keep YouTube cookies fresh on the server so cobalt +
   yt-dlp can fetch authenticated content (the entire v0.1 feature, preserved
   verbatim).

> **v0.2.0 dev note:** The Phase 1 refactor (this version) reorganizes the
> code into the multitool layout but ships **only** the v0.1 cookie-sync
> behavior — no popup capture, no context menu, no pills. Each Phase 2–9
> feature lights up incrementally. See
> [`docs/plans/2026-05-10-browser-extension-multitool-macro-plan.md`](../docs/plans/2026-05-10-browser-extension-multitool-macro-plan.md).
```

Keep the rest of the README's install / verify / security / icon sections — they're still accurate for the cookie subsystem. Replace any remaining `Affine YT Cookie Sync` literal with `AFFiNE Capture`.

- [ ] **Step 2: Manual acceptance — full Phase 1 smoke test**

Run through the spec §8 + macro plan Phase 1 acceptance criteria:

- [ ] `chrome://extensions/` → "Load unpacked" the `browser-extension/` folder → shows as **AFFiNE Capture v0.2.0** with the cookie icon.
- [ ] Click toolbar icon → popup loads with v0.1 cookie status UI intact, no console errors.
- [ ] Open options page → fill `ingestUrl` + `ingestToken` → click Save → fields persist.
- [ ] Click "Sync now" → status flips to "Synced 1s ago — N cookies".
- [ ] `docker logs affine_ingest --tail 5 --since 1m | grep "youtube cookies"` → log line present, `byte_count` matches.
- [ ] Server status row appears: "Server: fresh (uploaded 0 min ago)".
- [ ] In service-worker console (`chrome://extensions/` → AFFiNE Capture → "Inspect views: service worker") run `chrome.alarms.getAll(console.log)` → see at least `yt-cookie-daily-sync` alarm.
- [ ] Visit `youtube.com` and log in (or out + back in) → service-worker console shows the cookies onChanged listener firing → 30 seconds later, a debounced sync runs.
- [ ] Stop the ingest container (`docker stop affine_ingest`) → wait 24 h (or fake the staleness via tmpfs file mtime) → toolbar shows red "!" badge → click extension → popup shows "Server: cookies missing. Likely an ingest restart — click Sync now."
- [ ] Restart ingest → click Sync now → red badge clears.
- [ ] All vitest tests pass: `cd browser-extension && npm test` → 3 test files, 18+ assertions, all green.

Any failure here = unfinished Phase 1. Fix before declaring complete.

- [ ] **Step 3: Commit README**

```sh
git add browser-extension/README.md
git commit -m "docs(extension): README v0.2 reflects multitool identity (phase 1.8)"
```

---

## Phase 1 done. Next:

Phase 2 macro-plan entry → write `docs/plans/2026-05-1X-phase-2-capture-client-popup.md` via `superpowers:writing-plans`, then dispatch.
