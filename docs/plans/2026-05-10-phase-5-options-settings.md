# Phase 5: Options page — Settings tab

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Replace the v0.1 single-page options form with a three-tab layout (Settings · History · Cookies). Wire the Settings tab fully (URL + bearer token + paste button + Test connection → green/red dot + version + Save). The History and Cookies tabs are placeholder content for now — Phase 6 fills History/Detail and Phase 7 fills Cookies.

**Spec:** [`docs/specs/2026-05-10-browser-extension-multitool-design.md`](../specs/2026-05-10-browser-extension-multitool-design.md) §4.2 (Settings)

**Macro plan:** Phase 5 in [`docs/plans/2026-05-10-browser-extension-multitool-macro-plan.md`](2026-05-10-browser-extension-multitool-macro-plan.md)

**Architecture:** Single options.html page with a top tab bar (`<nav>`); URL hash routes (`#settings` / `#history` / `#cookies`) drive tab visibility. Settings tab uses Web Components from Phase 4 (`<af-button>`, `<af-input>`, `<af-card>`). The "Test connection" button calls `lib/api.js`'s `request('GET', '/health')`.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `browser-extension/lib/api.js` | Modify | Add `health()` convenience wrapper. |
| `browser-extension/options/options.html` | Rewrite | Three-tab layout w/ nav, Settings panel using Web Components, History + Cookies placeholders. |
| `browser-extension/options/options.css` | Create | Page layout (max-width 960 px), tab bar styling, panel transitions. |
| `browser-extension/options/options.js` | Rewrite | Tab routing (URL hash), Settings form load/save/test, Web Component imports. |

---

## Task 1: Add `health()` to `lib/api.js`

**Files:**
- Modify: `browser-extension/lib/api.js`
- Modify: `browser-extension/lib/__tests__/api.test.js` (append 1 test)

- [ ] **Step 1: Append failing test**

Add after the last test in `api.test.js`:

```js
describe('lib/api.health', () => {
  beforeEach(() => {
    vi.spyOn(storage, 'getConfig').mockResolvedValue({
      ingestUrl: 'https://ingest.test',
      ingestToken: 'tok',
      extendedScope: false,
    });
    globalThis.fetch = vi.fn();
  });

  it('GET /health returns parsed body', async () => {
    fetch.mockResolvedValue(new Response(JSON.stringify({
      ok: true, queue_depth: 2, worker_alive: true, version: '0.1.0',
    }), { status: 200, headers: { 'content-type': 'application/json' } }));
    const result = await health();
    expect(result).toEqual({ ok: true, queue_depth: 2, worker_alive: true, version: '0.1.0' });
  });
});
```

Update the import line at the top to include `health`:

```js
import { request, IngestError, health } from '../api.js';
```

- [ ] **Step 2: Run — fails**

```sh
cd C:/Users/PC/Projects/ToEverything/portainer-stack/browser-extension
npm test -- lib/__tests__/api.test.js
```

Expected: existing 9 pass, new one fails ("health is not a function").

- [ ] **Step 3: Add `health()` to `lib/api.js`**

After the `request` function, add:

```js
/**
 * Convenience wrapper for the health probe. Returns
 * `{ok: bool, queue_depth: int, worker_alive: bool, version: string}`.
 * Throws IngestError on auth/network errors (caller should distinguish
 * via err.kind to render green/red dot).
 */
export async function health() {
  return await request('GET', '/health');
}
```

- [ ] **Step 4: Run — passes**

```sh
npm test -- lib/__tests__/api.test.js
```

Expected: 10 / 10 passing in api.test.js. Total project: 50 / 50 (49 prior + 1 new).

- [ ] **Step 5: Commit**

```sh
git add browser-extension/lib/api.js browser-extension/lib/__tests__/api.test.js
git commit -m "feat(extension): lib/api.health() convenience wrapper (phase 5.1)"
```

---

## Task 2: Three-tab `options.html` + `options.css`

This task lays out the new options page structure but leaves the Settings tab JS for Task 3, and History + Cookies as placeholders for Phases 6/7.

**Files:**
- Modify: `browser-extension/options/options.html` (full rewrite)
- Create: `browser-extension/options/options.css`

- [ ] **Step 1: Replace `options/options.html`**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>AFFiNE Capture — Settings</title>
  <link rel="stylesheet" href="../lib/design-tokens.css">
  <link rel="stylesheet" href="options.css">
</head>
<body>
  <div class="page">
    <header class="page-head">
      <h1>AFFiNE Capture</h1>
      <p class="subtitle">Browser extension for your self-hosted ingest service</p>
    </header>

    <nav class="tabs" role="tablist">
      <a href="#settings" class="tab" data-tab="settings" role="tab">Settings</a>
      <a href="#history"  class="tab" data-tab="history"  role="tab">History</a>
      <a href="#cookies"  class="tab" data-tab="cookies"  role="tab">Cookies</a>
    </nav>

    <main class="panels">
      <!-- Settings panel -->
      <section id="panel-settings" class="panel" role="tabpanel" data-panel="settings">
        <af-card>
          <h2>Server</h2>
          <p class="hint">Where AFFiNE Capture sends your captures.</p>

          <label class="field-label" for="ingestUrl">Ingest base URL</label>
          <af-input id="ingestUrl" type="url" placeholder="https://ingest.example.com:3200"></af-input>

          <label class="field-label" for="ingestToken">Bearer token</label>
          <af-input id="ingestToken" type="password" placeholder="ut_… (your INGEST_API_TOKEN)" paste-button></af-input>

          <div class="actions">
            <af-button id="testConnection" variant="secondary">Test connection</af-button>
            <af-button id="save" variant="primary">Save</af-button>
            <span id="testResult" class="test-result" hidden></span>
          </div>
        </af-card>
      </section>

      <!-- History panel — placeholder until Phase 6 -->
      <section id="panel-history" class="panel" role="tabpanel" data-panel="history" hidden>
        <af-card>
          <h2>History</h2>
          <p class="hint">Recent captures appear here. Coming in Phase 6.</p>
        </af-card>
      </section>

      <!-- Cookies panel — placeholder until Phase 7 -->
      <section id="panel-cookies" class="panel" role="tabpanel" data-panel="cookies" hidden>
        <af-card>
          <h2>YouTube cookies</h2>
          <p class="hint">Cookie sync status and controls. Coming in Phase 7.</p>
          <p class="hint" style="margin-top: var(--af-space-2);">
            Until Phase 7, the v0.1 options page (now superseded) was the only place
            to trigger a manual sync. Auto-sync still runs on cookie change + daily.
          </p>
        </af-card>
      </section>
    </main>
  </div>

  <script type="module" src="options.js"></script>
</body>
</html>
```

- [ ] **Step 2: Create `options/options.css`**

```css
/* AFFiNE Capture — options page (spec §4.2) */

body {
  margin: 0;
  background: var(--af-gray-50);
  color: var(--af-navy);
  font: var(--af-body);
  font-family: var(--af-font);
}

.page {
  max-width: 960px;
  margin: 0 auto;
  padding: var(--af-space-5) var(--af-space-4);
}

.page-head {
  margin-bottom: var(--af-space-4);
}
.page-head h1 {
  font: var(--af-h2);
  margin: 0 0 var(--af-space-1);
}
.page-head .subtitle {
  margin: 0;
  color: var(--af-text-body);
  font: var(--af-small);
}

.tabs {
  display: flex;
  gap: var(--af-space-1);
  margin-bottom: var(--af-space-4);
  border-bottom: 1px solid var(--af-border);
}
.tab {
  padding: var(--af-space-2) var(--af-space-3);
  font: var(--af-small);
  font-weight: 600;
  color: var(--af-text-body);
  text-decoration: none;
  border-bottom: 2px solid transparent;
  transition: color .15s ease, border-color .15s ease;
}
.tab:hover { color: var(--af-navy); }
.tab.active {
  color: var(--af-blue);
  border-bottom-color: var(--af-blue);
}

.panel {
  display: block;
}
.panel[hidden] { display: none; }

.panel h2 {
  font: var(--af-h3);
  margin: 0 0 var(--af-space-2);
}
.panel .hint {
  margin: 0 0 var(--af-space-3);
  color: var(--af-text-body);
  font: var(--af-small);
}

.field-label {
  display: block;
  margin-top: var(--af-space-3);
  margin-bottom: var(--af-space-1);
  font: var(--af-small);
  font-weight: 600;
  color: var(--af-navy);
}

.actions {
  display: flex;
  align-items: center;
  gap: var(--af-space-2);
  margin-top: var(--af-space-4);
}

.test-result {
  display: inline-flex;
  align-items: center;
  gap: var(--af-space-1);
  font: var(--af-small);
}
.test-result.ok    { color: var(--af-success); }
.test-result.err   { color: var(--af-error); }
.test-result::before {
  content: '';
  width: 8px; height: 8px;
  border-radius: 50%;
  background: currentColor;
}

.toast {
  position: fixed;
  bottom: var(--af-space-4);
  left: 50%;
  transform: translateX(-50%);
  padding: var(--af-space-2) var(--af-space-4);
  background: var(--af-navy);
  color: var(--af-surface);
  border-radius: var(--af-radius-button);
  font: var(--af-small);
  box-shadow: var(--af-shadow-card);
  opacity: 0;
  transition: opacity .2s ease;
  pointer-events: none;
}
.toast.visible { opacity: 1; }
```

- [ ] **Step 3: Tests still green**

```sh
npm test
```

Expected: 50 / 50 (no test changes — Task 1 added 1 to make 50; this task adds no tests).

- [ ] **Step 4: Commit**

```sh
git add browser-extension/options/options.html browser-extension/options/options.css
git commit -m "feat(extension): three-tab options layout + design-system styling (phase 5.2)"
```

---

## Task 3: `options.js` — tab routing + Settings form behavior

**Files:**
- Modify: `browser-extension/options/options.js` (full rewrite)

- [ ] **Step 1: Replace `options/options.js`**

```js
/**
 * AFFiNE Capture — options page.
 *
 * Three tabs: Settings (this phase) · History (Phase 6) · Cookies (Phase 7).
 * URL hash routes drive tab visibility. The Settings tab persists URL + token
 * to chrome.storage.local via lib/storage.js, and Test connection hits the
 * health endpoint via lib/api.js.
 */
import '../options/components/af-button.js';
import '../options/components/af-input.js';
import '../options/components/af-card.js';
import { getConfig, setConfig } from '../lib/storage.js';
import { health, IngestError } from '../lib/api.js';

const VALID_TABS = ['settings', 'history', 'cookies'];

const $tabs = document.querySelectorAll('.tab');
const $panels = document.querySelectorAll('.panel');
const $url = document.getElementById('ingestUrl');
const $token = document.getElementById('ingestToken');
const $test = document.getElementById('testConnection');
const $save = document.getElementById('save');
const $testResult = document.getElementById('testResult');

routeFromHash();
window.addEventListener('hashchange', routeFromHash);

loadSettings();

$test.addEventListener('click', testConnection);
$save.addEventListener('click', saveSettings);

function currentTab() {
  const hash = window.location.hash.replace('#', '');
  return VALID_TABS.includes(hash) ? hash : 'settings';
}

function routeFromHash() {
  const target = currentTab();
  for (const tab of $tabs) {
    tab.classList.toggle('active', tab.dataset.tab === target);
  }
  for (const panel of $panels) {
    panel.hidden = panel.dataset.panel !== target;
  }
}

async function loadSettings() {
  const cfg = await getConfig();
  // Wait a microtask so the Web Components have wired their value setters.
  await new Promise(r => requestAnimationFrame(r));
  $url.value = cfg.ingestUrl ?? '';
  $token.value = cfg.ingestToken ?? '';
}

async function testConnection() {
  $testResult.hidden = false;
  $testResult.className = 'test-result';
  $testResult.textContent = 'Testing…';
  // Save current values first so health() reads them via storage.
  await setConfig({ ingestUrl: $url.value.trim(), ingestToken: $token.value.trim() });
  try {
    const res = await health();
    if (res?.ok) {
      $testResult.classList.add('ok');
      $testResult.textContent = `OK · v${res.version ?? '?'} · queue ${res.queue_depth ?? 0}`;
    } else {
      $testResult.classList.add('err');
      $testResult.textContent = 'Server replied not-ok';
    }
  } catch (e) {
    $testResult.classList.add('err');
    if (e instanceof IngestError) {
      $testResult.textContent = errorLabel(e);
    } else {
      $testResult.textContent = e?.message ?? 'Failed';
    }
  }
}

async function saveSettings() {
  const url = $url.value.trim();
  const token = $token.value.trim();
  await setConfig({ ingestUrl: url || null, ingestToken: token || null });
  showToast('Saved');
}

function errorLabel(err) {
  switch (err.kind) {
    case 'invalid_token':  return 'Token rejected';
    case 'config':         return 'URL / token not configured';
    case 'rate_limited':   return `Rate limited (${err.retryAfter ?? '?'}s)`;
    case 'network':        return `Couldn't reach server`;
    case 'server':         return `Server error ${err.status ?? ''}`.trim();
    default:               return err.message ?? 'Failed';
  }
}

function showToast(text) {
  let toast = document.querySelector('.toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.className = 'toast';
    document.body.appendChild(toast);
  }
  toast.textContent = text;
  toast.classList.add('visible');
  setTimeout(() => toast.classList.remove('visible'), 2000);
}
```

- [ ] **Step 2: Tests still green**

```sh
npm test
```

Expected: 50 / 50 (options.js is integration-tested by manual smoke; no unit tests added).

- [ ] **Step 3: Commit**

```sh
git add browser-extension/options/options.js
git commit -m "feat(extension): options page tab routing + Settings form (phase 5.3)"
```

---

## Task 4: Manual smoke (USER)

After Tasks 1–3 land:

- [ ] Reload extension. Right-click toolbar icon → "Options" → new tabbed page opens.
- [ ] Three tabs visible: Settings (active), History, Cookies.
- [ ] Click each tab → URL hash updates (`#settings` / `#history` / `#cookies`); only the active panel is visible.
- [ ] Settings shows pre-filled URL + token (from prior config).
- [ ] Token input has a "Paste" button on the right edge.
- [ ] Click "Test connection" with valid token → `OK · vX.Y.Z · queue N` within 2s.
- [ ] Edit token to a bad value → click Test → "Token rejected" within 2s.
- [ ] Click Save → toast "Saved" appears + disappears in ~2s.
- [ ] Refresh page → Settings tab is still default; if URL was `…/options.html#history`, History tab is selected on load.
- [ ] Popup capture from a tab → still works (Settings tab is the source of truth for URL/token; saving updates storage that popup reads).
