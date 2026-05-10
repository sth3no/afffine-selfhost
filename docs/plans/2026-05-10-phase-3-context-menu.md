# Phase 3: Context menu (page / link / selection / image)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Right-click anywhere → "Save … to AFFiNE" with four context-aware items (page, link, selection, image). Successful capture surfaces a `chrome.notifications` toast; clicking the notification opens the AFFiNE doc in a new tab. Failures show an error toast.

**Spec:** [`docs/specs/2026-05-10-browser-extension-multitool-design.md`](../specs/2026-05-10-browser-extension-multitool-design.md) §4.3, §7

**Macro plan:** Phase 3 in [`docs/plans/2026-05-10-browser-extension-multitool-macro-plan.md`](2026-05-10-browser-extension-multitool-macro-plan.md)

**Architecture note:** Phase 2's `payload.js` already accepts an `info` arg — its 6 tests cover page/link/selection/image. So **no `payload.js` changes are needed**. Phase 3 reuses what's there.

We do refactor `background.js`'s `handleCapture` into `capture/handler.js` so both the popup `'capture'` message AND context-menu clicks can call the same function without duplicating storage/badge logic.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `browser-extension/capture/handler.js` | Create | `performCapture(payload)` + `toRecentRow()` extracted from `background.js`. Single source of truth for the capture flow side-effects (storage, badge). |
| `browser-extension/capture/context-menu.js` | Create | `registerContextMenus()` + `handleContextMenuClick(info, tab)` — registers 4 menu items, handles clicks, shows notifications. |
| `browser-extension/background.js` | Modify | Use `performCapture` from `capture/handler.js`. Wire `registerContextMenus` into `onInstalled` and `chrome.contextMenus.onClicked` into `handleContextMenuClick`. |

---

## Task 1: Extract `performCapture` into `capture/handler.js`

Pure refactor — moves logic from `background.js` into a reusable module. No behavior change.

**Files:**
- Create: `browser-extension/capture/handler.js`
- Modify: `browser-extension/background.js`

- [ ] **Step 1: Create `capture/handler.js`**

```js
/**
 * Capture flow side-effects: POST /capture, persist lastResult, prepend to
 * recentCaptures cache, update capture-subsystem badge.
 *
 * Pure function over the storage + badge modules — used by both the popup
 * 'capture' message handler (background.js) and the context-menu click
 * handler (capture/context-menu.js).
 */
import { captureUrl } from './client.js';
import { setLastResult, getRecentCaptures, setRecentCaptures } from '../lib/storage.js';
import { setSubsystem } from '../lib/badge.js';
import { IngestError } from '../lib/api.js';

/**
 * @param {{url?: string, source_app?: string|null, shared_title?: string,
 *           shared_text?: string}} payload
 * @returns {Promise<{ok: true, capture_id: string, doc_id: string,
 *           web_url: string, status: string, platform: string,
 *           initial_path: string, created_at: string} |
 *           {ok: false, error: {kind: string, message: string, status?: number}}>}
 */
export async function performCapture(payload) {
  try {
    const response = await captureUrl(payload);
    const result = { ok: true, ...response };
    await setLastResult(result);
    const recent = await getRecentCaptures();
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

- [ ] **Step 2: Update `background.js` to use `performCapture`**

Replace the `handleCapture` and `toRecentRow` function definitions (~30 lines) at the bottom of `background.js` — they now live in `capture/handler.js`. Also drop the now-unused imports (`captureUrl`, `setLastResult`, `getRecentCaptures`, `setRecentCaptures`, `setSubsystem`, `IngestError`).

The new background.js after this change:

- Imports stay: `syncCookies` from cookies/sync, `refreshBadge` from lib/badge.
- New import: `import { performCapture } from './capture/handler.js';`
- The `chrome.runtime.onMessage` listener's `'capture'` branch becomes:

```js
if (msg?.type === 'capture') {
  performCapture(msg.payload).then(sendResponse);
  return true;
}
```

- Remove the old `handleCapture` and `toRecentRow` function definitions entirely.

- [ ] **Step 3: Tests pass**

```sh
cd C:/Users/PC/Projects/ToEverything/portainer-stack/browser-extension
npm test
```

Expected: still 24 / 24 — no test changes, just reorganization.

- [ ] **Step 4: Commit**

```sh
git add browser-extension/capture/handler.js browser-extension/background.js
git commit -m "refactor(extension): extract performCapture into capture/handler.js (phase 3.1)"
```

---

## Task 2: Context menu + notifications

**Files:**
- Create: `browser-extension/capture/context-menu.js`
- Modify: `browser-extension/background.js`

- [ ] **Step 1: Create `capture/context-menu.js`**

```js
/**
 * Context-menu integration. On install, registers four menu items
 * (page / link / selection / image). On click, builds a CaptureRequest
 * payload and dispatches via performCapture(), then shows a notification.
 *
 * Clicking the notification opens the AFFiNE doc URL in a new tab.
 */
import { buildPayloadFromTab } from './payload.js';
import { performCapture } from './handler.js';

const MENU_IDS = {
  page: 'affine-capture-page',
  link: 'affine-capture-link',
  selection: 'affine-capture-selection',
  image: 'affine-capture-image',
};

/**
 * Map from notification ID to web_url. Lets onClicked open the right doc.
 * Service-worker memory only; cleared when the worker dies — that's OK,
 * notifications are short-lived UI.
 */
const notificationOpenUrls = new Map();

/**
 * Register the four context menu items. Idempotent — safe to call on
 * every onInstalled (chrome.contextMenus removes existing items with the
 * same ID via removeAll first).
 */
export function registerContextMenus() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU_IDS.page,
      title: 'Save page to AFFiNE',
      contexts: ['page'],
    });
    chrome.contextMenus.create({
      id: MENU_IDS.link,
      title: 'Save link to AFFiNE',
      contexts: ['link'],
    });
    chrome.contextMenus.create({
      id: MENU_IDS.selection,
      title: 'Save selection to AFFiNE',
      contexts: ['selection'],
    });
    chrome.contextMenus.create({
      id: MENU_IDS.image,
      title: 'Save image to AFFiNE',
      contexts: ['image'],
    });
  });
}

/**
 * Handle a context menu click: build payload, perform capture, notify.
 */
export async function handleContextMenuClick(info, tab) {
  if (!tab) return;
  if (!Object.values(MENU_IDS).includes(info.menuItemId)) return;

  const payload = buildPayloadFromTab(tab, info);
  const result = await performCapture(payload);

  if (result.ok) {
    showSuccessNotification(result);
  } else {
    showErrorNotification(result.error);
  }
}

/**
 * Wire the notification-clicked listener so a click on the success toast
 * opens the AFFiNE doc URL in a new tab.
 */
export function registerNotificationHandlers() {
  chrome.notifications.onClicked.addListener(notificationId => {
    const url = notificationOpenUrls.get(notificationId);
    if (url) {
      chrome.tabs.create({ url });
      chrome.notifications.clear(notificationId);
      notificationOpenUrls.delete(notificationId);
    }
  });
  chrome.notifications.onClosed.addListener(notificationId => {
    notificationOpenUrls.delete(notificationId);
  });
}

function showSuccessNotification(response) {
  const id = `affine-capture-${response.capture_id}`;
  notificationOpenUrls.set(id, response.web_url);
  chrome.notifications.create(id, {
    type: 'basic',
    iconUrl: chrome.runtime.getURL('icons/icon-128.png'),
    title: 'Saved to AFFiNE',
    message: `${response.platform} · ${response.initial_path}\nClick to open`,
    priority: 0,
  });
}

function showErrorNotification(err) {
  const title = err.kind === 'invalid_token'
    ? 'Token rejected'
    : err.kind === 'config'
    ? 'AFFiNE Capture not configured'
    : err.kind === 'rate_limited'
    ? 'Rate limited'
    : err.kind === 'network'
    ? 'Couldn\'t reach ingest'
    : 'Capture failed';
  chrome.notifications.create({
    type: 'basic',
    iconUrl: chrome.runtime.getURL('icons/icon-128.png'),
    title,
    message: err.message ?? 'See AFFiNE Capture for details',
    priority: 1,
  });
}
```

- [ ] **Step 2: Wire it into `background.js`**

Add to the imports at the top:

```js
import {
  registerContextMenus,
  handleContextMenuClick,
  registerNotificationHandlers,
} from './capture/context-menu.js';
```

Extend `chrome.runtime.onInstalled` to register menus:

```js
chrome.runtime.onInstalled.addListener(() => {
  syncCookies();
  chrome.alarms.create(ALARM_DAILY_SYNC, { periodInMinutes: 60 * 24 });
  registerContextMenus();
});
```

Add the contextMenus click listener and notification handlers as new top-level statements (alongside the existing `chrome.cookies.onChanged.addListener`):

```js
chrome.contextMenus.onClicked.addListener((info, tab) => {
  handleContextMenuClick(info, tab);
});

registerNotificationHandlers();
```

- [ ] **Step 3: Tests still pass**

```sh
npm test
```

Expected: 24 / 24 (no new tests for context-menu — chrome.contextMenus is hard to test without an end-to-end harness, covered by manual smoke).

- [ ] **Step 4: Commit**

```sh
git add browser-extension/capture/context-menu.js browser-extension/background.js
git commit -m "feat(extension): context-menu items + notification feedback (phase 3.2)"
```

---

## Task 3: Manual smoke (USER)

After Tasks 1+2 land, the user reloads the extension and checks:

- [ ] Right-click on any web page → see exactly **one** "Save page to AFFiNE" menu item (not multiple — proves `removeAll` cleanup works).
- [ ] Right-click "Save page" → notification toast within 2s with title "Saved to AFFiNE", body "platform · initial_path · Click to open".
- [ ] Click the notification body → AFFiNE doc opens in a new tab.
- [ ] Right-click on a link → menu shows "Save link to AFFiNE"; clicking captures the link's `href`, not the page URL. Verify in History (next phase) or by checking server logs.
- [ ] Highlight some text → right-click → "Save selection to AFFiNE" present; click captures `shared_text` containing the highlighted text.
- [ ] Right-click on an image → "Save image to AFFiNE" present; click captures the image src URL.
- [ ] Bogus token → context-menu click → notification "Token rejected". Toolbar badge shows red `!`.
- [ ] Notifications dismissed do not leak the URL map (just smoke-verify by clicking + dismissing a few without crashing).

Report any failures back here.
