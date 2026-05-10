# Phase 7: Options page — Cookies tab (restyled)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan.

**Goal:** Move the v0.1 cookie-sync UI (last sync, server verdict, Sync now button, extended-scope checkbox) into the new Cookies tab of the options page, restyled with the design system. **No behavior change** — same `chrome.runtime.sendMessage('sync-now')`, same `chrome.permissions.request/remove`, same `chrome.storage.local.lastSync` shape.

**Spec:** [`docs/specs/2026-05-10-browser-extension-multitool-design.md`](../specs/2026-05-10-browser-extension-multitool-design.md) §4.2 (Cookies)

**Macro plan:** Phase 7 in [`docs/plans/2026-05-10-browser-extension-multitool-macro-plan.md`](2026-05-10-browser-extension-multitool-macro-plan.md)

---

## Task 1: Cookies tab content + handlers (single commit)

**Files:**
- Modify: `browser-extension/options/options.html` (replace Cookies panel placeholder)
- Modify: `browser-extension/options/options.css` (append cookie-status styles)
- Modify: `browser-extension/options/options.js` (add cookie handlers, hook into `routeFromHash`)

### Step 1: Replace the Cookies panel in `options.html`

Find this existing block (placeholder from Phase 5):

```html
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
```

Replace its inner content with:

```html
<section id="panel-cookies" class="panel" role="tabpanel" data-panel="cookies" hidden>
  <af-card>
    <h2>YouTube cookie sync</h2>
    <p class="hint">
      Pushes your YouTube cookies (from this browser) to the ingest service so
      cobalt + yt-dlp can fetch authenticated content. Auto-syncs on cookie change
      and once a day; click "Sync now" to force.
    </p>

    <div id="cookieStatusCard" class="cookie-status-card">
      <div class="cookie-status-row">
        <span class="cookie-status-label">Last sync (browser):</span>
        <span id="cookieLastSync">never</span>
      </div>
      <div class="cookie-status-row">
        <span class="cookie-status-label">Server verdict:</span>
        <span id="cookieServerStatus" class="cookie-verdict">unknown</span>
      </div>
    </div>

    <div class="actions">
      <af-button id="syncNow" variant="primary">Sync now</af-button>
      <span id="syncResult" class="test-result" hidden></span>
    </div>

    <label class="checkbox-row">
      <input type="checkbox" id="extendedScope">
      <span>
        Also include <code>accounts.google.com</code> cookies
        <span class="hint">— helps with age-gated and members-only videos. Browser will prompt for permission.</span>
      </span>
    </label>
  </af-card>
</section>
```

### Step 2: Append styles to `options.css`

```css
/* Cookies tab */

.cookie-status-card {
  background: var(--af-bg-soft);
  border-radius: var(--af-radius-button);
  padding: var(--af-space-3);
  margin: var(--af-space-3) 0;
}
.cookie-status-row {
  display: flex;
  align-items: center;
  gap: var(--af-space-2);
  font: var(--af-small);
}
.cookie-status-row + .cookie-status-row {
  margin-top: var(--af-space-1);
}
.cookie-status-label {
  color: var(--af-text-body);
  min-width: 140px;
}
.cookie-verdict.fresh   { color: var(--af-success); font-weight: 600; }
.cookie-verdict.stale   { color: #856404; font-weight: 600; }
.cookie-verdict.missing,
.cookie-verdict.error   { color: var(--af-error); font-weight: 600; }

.checkbox-row {
  display: flex;
  align-items: flex-start;
  gap: var(--af-space-2);
  margin-top: var(--af-space-4);
  font: var(--af-small);
  color: var(--af-navy);
}
.checkbox-row input[type=checkbox] {
  margin-top: 2px;
  accent-color: var(--af-blue);
}
.checkbox-row code {
  background: var(--af-gray-50);
  padding: 1px 4px;
  border-radius: 4px;
  font: var(--af-small);
  font-family: var(--af-font-mono);
}
.checkbox-row .hint {
  display: inline;
  margin: 0;
}
```

### Step 3: Extend `options.js`

Add to imports (just `getLastSync` from storage):

```js
import { getConfig, setConfig, getRecentCaptures, getLastSync } from '../lib/storage.js';
```

Update `routeFromHash` to render Cookies tab when active:

```js
function routeFromHash() {
  const target = currentTab();
  for (const tab of $tabs) {
    tab.classList.toggle('active', tab.dataset.tab === target);
  }
  for (const panel of $panels) {
    panel.hidden = panel.dataset.panel !== target;
  }
  if (target === 'history') renderHistoryView();
  if (target === 'cookies') renderCookiesView();
}
```

Append the cookies view module at the end of the file:

```js
const $cookieLastSync = document.getElementById('cookieLastSync');
const $cookieServerStatus = document.getElementById('cookieServerStatus');
const $syncNow = document.getElementById('syncNow');
const $syncResult = document.getElementById('syncResult');
const $extendedScope = document.getElementById('extendedScope');

$syncNow.addEventListener('click', async () => {
  $syncResult.hidden = false;
  $syncResult.className = 'test-result';
  $syncResult.textContent = 'Syncing…';
  const result = await chrome.runtime.sendMessage({ type: 'sync-now' });
  if (result?.ok) {
    $syncResult.classList.add('ok');
    $syncResult.textContent = `Synced — ${result.cookie_count} cookies`;
  } else {
    $syncResult.classList.add('err');
    $syncResult.textContent = `Failed: ${result?.error ?? 'unknown'}`;
  }
  await renderCookiesView();  // refresh the status card
});

$extendedScope.addEventListener('change', async () => {
  if ($extendedScope.checked) {
    const granted = await chrome.permissions.request({
      origins: ['*://accounts.google.com/*', '*://*.google.com/*'],
    });
    if (!granted) {
      $extendedScope.checked = false;
      return;
    }
    await setConfig({ extendedScope: true });
  } else {
    await chrome.permissions.remove({
      origins: ['*://accounts.google.com/*', '*://*.google.com/*'],
    });
    await setConfig({ extendedScope: false });
  }
});

async function renderCookiesView() {
  // Last sync (browser side) + server verdict.
  const lastSync = await getLastSync();
  if (!lastSync) {
    $cookieLastSync.textContent = 'never';
    $cookieServerStatus.textContent = 'unknown';
    $cookieServerStatus.className = 'cookie-verdict';
  } else if (!lastSync.ok) {
    $cookieLastSync.textContent = 'failed';
    $cookieServerStatus.textContent = lastSync.error ?? 'failed';
    $cookieServerStatus.className = 'cookie-verdict error';
  } else {
    const ago = formatRelativeOptions(new Date(lastSync.synced_at));
    $cookieLastSync.textContent = `${ago} (${lastSync.cookie_count} cookies)`;
    const verdict = lastSync.verdict ?? 'unknown';
    $cookieServerStatus.textContent = verdictLabel(verdict, lastSync.server_status);
    $cookieServerStatus.className = `cookie-verdict ${verdict}`;
  }
  // Extended-scope checkbox state from storage (and verify perm still held).
  const cfg = await getConfig();
  $extendedScope.checked = !!cfg.extendedScope;
}

function verdictLabel(verdict, serverStatus) {
  if (verdict === 'fresh') {
    const ageMin = Math.floor((serverStatus?.age_seconds ?? 0) / 60);
    return `fresh (uploaded ${ageMin} min ago)`;
  }
  if (verdict === 'stale') {
    const ageH = Math.floor((serverStatus?.age_seconds ?? 0) / 3600);
    return `stale (cookies ${ageH}h old)`;
  }
  if (verdict === 'missing') return 'cookies missing on server';
  return 'unknown';
}

function formatRelativeOptions(date) {
  const sec = Math.floor((Date.now() - date.getTime()) / 1000);
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}
```

(Note: `formatRelativeOptions` deliberately scopes to options.js — the popup has its own `formatRelative`, and the Web Components have their own. Each is small enough that DRY-into-a-shared-module is more ceremony than benefit at this stage.)

### Step 4: Tests still green (61/61)

```sh
cd C:/Users/PC/Projects/ToEverything/portainer-stack/browser-extension
npm test
```

### Step 5: Commit

```sh
git add browser-extension/options/options.html browser-extension/options/options.css browser-extension/options/options.js
git commit -m "feat(extension): Cookies tab with sync status, manual sync, extended-scope toggle (phase 7.1)"
```

---

## Task 2: Manual smoke (USER)

- [ ] Open options → Cookies tab → see "Last sync" + "Server verdict" rows.
- [ ] Click "Sync now" → "Syncing…" briefly → "Synced — N cookies" within 2s.
- [ ] Server verdict updates: "fresh (uploaded 0 min ago)" right after sync.
- [ ] Toggle extended-scope checkbox → browser prompts for `accounts.google.com` permission → tick stays checked if granted.
- [ ] Untick → permission removed (verify in `chrome://extensions` → Details → Site access).
- [ ] No regression in popup: cookie footer there still updates from same `lastSync` shape.
- [ ] Reload options on `…/options.html#cookies` directly → Cookies tab loads on first paint with status filled in.
