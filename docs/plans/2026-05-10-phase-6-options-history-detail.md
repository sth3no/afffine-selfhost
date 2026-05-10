# Phase 6: Options page — History + Detail sub-view

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Fill in the History tab of the options page. List recent captures with platform icons + status badges + relative time + filter pills + hover Retry/Delete. Click a row → Detail sub-view with status timeline, classifier_reasoning, breadcrumb, Open/Retry/Delete actions.

**Spec:** [`docs/specs/2026-05-10-browser-extension-multitool-design.md`](../specs/2026-05-10-browser-extension-multitool-design.md) §4.2 (History + Detail)

**Macro plan:** Phase 6 in [`docs/plans/2026-05-10-browser-extension-multitool-macro-plan.md`](2026-05-10-browser-extension-multitool-macro-plan.md)

**Architecture:** History tab seeds from `recentCaptures` cache (instant render), then refreshes from `GET /captures?limit=50`. Detail view is a sub-route of History — `#history` shows the list, `#history/<capture_id>` shows the detail. Click row navigates via `location.hash`. Retry/Delete use `capture/client.js`. The list rows + status timeline + breadcrumb are 3 small Web Components for reusability.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `browser-extension/options/components/af-history-row.js` | Create | `<af-history-row>` — composed from `<af-status-badge>` + platform icon + title + topic_path + relative time + hover actions. |
| `browser-extension/options/components/af-status-timeline.js` | Create | `<af-status-timeline status>` — 5-step horizontal timeline (queued → extracting → classifying → filing → done). |
| `browser-extension/options/components/af-breadcrumb.js` | Create | `<af-breadcrumb path="Sources/Socials/Instagram/Recipes">` — slash-separated segments. |
| `browser-extension/options/components/__tests__/components.test.js` | Modify | Append tests for the 3 new components. |
| `browser-extension/options/options.html` | Modify | Replace the History panel placeholder with: filter pills + list container + empty state + Detail sub-panel (slides in). |
| `browser-extension/options/options.css` | Modify | History list styles + filter pills + detail panel layout. |
| `browser-extension/options/options.js` | Modify | Extend hash routing to handle `#history` (list) vs `#history/<id>` (detail); load + render list from cache + server; wire filter pills, Retry, Delete, Open actions. |

---

## Task 1: `<af-history-row>` + `<af-status-timeline>` + `<af-breadcrumb>` Web Components

**Files:**
- Create: `browser-extension/options/components/af-history-row.js`
- Create: `browser-extension/options/components/af-status-timeline.js`
- Create: `browser-extension/options/components/af-breadcrumb.js`
- Modify: `browser-extension/options/components/__tests__/components.test.js`

- [ ] **Step 1: Append tests for all three new components**

```js
import '../af-history-row.js';
import '../af-status-timeline.js';
import '../af-breadcrumb.js';

describe('<af-history-row>', () => {
  function makeRow(props = {}) {
    const el = document.createElement('af-history-row');
    el.data = {
      capture_id: '01ABC',
      platform: 'youtube',
      status: 'done',
      shared_title: 'My video',
      topic_path: 'Sources/Videos/YouTube',
      created_at: new Date().toISOString(),
      web_url: 'https://example.com/doc',
      ...props,
    };
    document.body.appendChild(el);
    return el;
  }

  it('registers', () => {
    expect(customElements.get('af-history-row')).toBeTypeOf('function');
  });

  it('renders title from data', () => {
    const el = makeRow({ shared_title: 'Hello world' });
    expect(el.shadowRoot.querySelector('.title')?.textContent).toBe('Hello world');
  });

  it('renders topic_path subtle below title', () => {
    const el = makeRow({ topic_path: 'Sources/X/Y' });
    expect(el.shadowRoot.querySelector('.path')?.textContent).toBe('Sources/X/Y');
  });

  it('emits "open" custom event on body click', () => {
    const el = makeRow();
    let received = null;
    el.addEventListener('open', e => { received = e.detail; });
    el.shadowRoot.querySelector('.body').click();
    expect(received?.capture_id).toBe('01ABC');
  });

  it('emits "retry" on retry button click', () => {
    const el = makeRow({ status: 'failed' });
    let received = null;
    el.addEventListener('retry', e => { received = e.detail; });
    el.shadowRoot.querySelector('button.retry')?.click();
    expect(received?.capture_id).toBe('01ABC');
  });

  it('emits "delete" on delete button click', () => {
    const el = makeRow();
    let received = null;
    el.addEventListener('delete', e => { received = e.detail; });
    el.shadowRoot.querySelector('button.delete')?.click();
    expect(received?.capture_id).toBe('01ABC');
  });
});

describe('<af-status-timeline>', () => {
  it('registers', () => {
    expect(customElements.get('af-status-timeline')).toBeTypeOf('function');
  });

  it('marks current step as active', () => {
    const el = document.createElement('af-status-timeline');
    el.setAttribute('status', 'classifying');
    document.body.appendChild(el);
    const steps = el.shadowRoot.querySelectorAll('.step');
    // queued, extracting, classifying are passed/active; filing/done not yet
    expect(steps.length).toBe(5);
    const active = el.shadowRoot.querySelector('.step.current');
    expect(active?.dataset.step).toBe('classifying');
  });

  it('marks failed status with error step', () => {
    const el = document.createElement('af-status-timeline');
    el.setAttribute('status', 'failed');
    document.body.appendChild(el);
    expect(el.shadowRoot.querySelector('.failed')).toBeTruthy();
  });
});

describe('<af-breadcrumb>', () => {
  it('registers', () => {
    expect(customElements.get('af-breadcrumb')).toBeTypeOf('function');
  });

  it('splits path into segments', () => {
    const el = document.createElement('af-breadcrumb');
    el.setAttribute('path', 'Sources/Socials/Instagram/Recipes');
    document.body.appendChild(el);
    const segments = el.shadowRoot.querySelectorAll('.segment');
    expect(segments.length).toBe(4);
    expect(segments[0].textContent).toBe('Sources');
    expect(segments[3].textContent).toBe('Recipes');
  });
});
```

- [ ] **Step 2: Run — fails**

```sh
cd C:/Users/PC/Projects/ToEverything/portainer-stack/browser-extension
npm test -- options/components/__tests__/components.test.js
```

Expected: 49 prior tests pass; 10 new fail.

- [ ] **Step 3: Implement `af-history-row.js`**

```js
/**
 * <af-history-row> — one row in the History tab.
 *
 * Properties (set via .data getter/setter, NOT attributes — capture rows
 * are objects with many fields):
 *   { capture_id, platform, status, shared_title?, topic_path?,
 *     created_at, web_url, doc_id }
 *
 * Events (composed: true so they bubble out of Shadow DOM):
 *   - 'open'   — body click; detail = full row data
 *   - 'retry'  — retry button click; detail = full row data
 *   - 'delete' — delete button click; detail = full row data
 */
import { platformIcon } from '../../lib/icons.js';
import { arrowClockwiseIcon, trashIcon } from '../../lib/icons.js';
import './af-status-badge.js';

const componentSheet = new CSSStyleSheet();
componentSheet.replaceSync(`
  :host {
    display: block;
    background: var(--af-surface);
    border: 1px solid var(--af-border);
    border-radius: var(--af-radius-card);
    margin-bottom: var(--af-space-2);
    transition: box-shadow .15s ease;
  }
  :host(:hover) { box-shadow: var(--af-shadow-card); }

  .row {
    display: grid;
    grid-template-columns: 24px 1fr auto auto;
    align-items: center;
    gap: var(--af-space-3);
    padding: var(--af-space-3);
  }

  .icon {
    color: var(--af-text-body);
    line-height: 0;
  }

  .body {
    cursor: pointer;
    display: flex;
    flex-direction: column;
    gap: 2px;
    overflow: hidden;
  }
  .title {
    font: var(--af-body);
    font-weight: 600;
    color: var(--af-navy);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .path {
    font: var(--af-small);
    color: var(--af-text-body);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .meta {
    display: flex;
    align-items: center;
    gap: var(--af-space-2);
    font: var(--af-small);
    color: var(--af-text-body);
    white-space: nowrap;
  }

  .actions {
    display: flex;
    gap: var(--af-space-1);
    opacity: 0;
    transition: opacity .15s ease;
  }
  :host(:hover) .actions { opacity: 1; }
  button.icon-btn {
    border: none;
    background: transparent;
    color: var(--af-text-body);
    cursor: pointer;
    padding: var(--af-space-1);
    border-radius: var(--af-radius-pill);
    line-height: 0;
  }
  button.icon-btn:hover {
    background: var(--af-bg-soft);
    color: var(--af-blue);
  }
  button.icon-btn.delete:hover { color: var(--af-error); }
`);

let tokenSheet = null;
async function getTokensSheet() {
  if (tokenSheet) return tokenSheet;
  const url = chrome.runtime.getURL('lib/design-tokens.css');
  const css = await (await fetch(url)).text();
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(css.replaceAll(':root', ':host'));
  tokenSheet = sheet;
  return sheet;
}

class AfHistoryRow extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.adoptedStyleSheets = [componentSheet];
    this._data = null;
  }

  set data(value) {
    this._data = value;
    if (this.shadowRoot.isConnected || this.isConnected) this._render();
  }
  get data() { return this._data; }

  async connectedCallback() {
    const sheet = await getTokensSheet();
    this.shadowRoot.adoptedStyleSheets = [sheet, componentSheet];
    this._render();
  }

  _render() {
    if (!this._data) return;
    const d = this._data;
    const isTerminal = d.status === 'done' || d.status === 'deleted';
    const showRetry = d.status === 'failed' || !isTerminal;
    this.shadowRoot.innerHTML = `
      <div class="row">
        <span class="icon">${platformIcon(d.platform)}</span>
        <div class="body">
          <div class="title">${escapeHtml(d.shared_title ?? d.url ?? '(untitled)')}</div>
          ${d.topic_path ? `<div class="path">${escapeHtml(d.topic_path)}</div>` : ''}
        </div>
        <span class="meta">
          <af-status-badge status="${escapeHtml(d.status)}"></af-status-badge>
          ${formatRelative(d.created_at)}
        </span>
        <span class="actions">
          ${showRetry ? `<button class="icon-btn retry" title="Retry">${arrowClockwiseIcon}</button>` : ''}
          <button class="icon-btn delete" title="Delete">${trashIcon}</button>
        </span>
      </div>
    `;
    // Re-bind because innerHTML wipes listeners.
    this.shadowRoot.querySelector('.body').addEventListener('click', () => {
      this.dispatchEvent(new CustomEvent('open', {
        detail: this._data, bubbles: true, composed: true,
      }));
    });
    const retry = this.shadowRoot.querySelector('button.retry');
    if (retry) retry.addEventListener('click', e => {
      e.stopPropagation();
      this.dispatchEvent(new CustomEvent('retry', {
        detail: this._data, bubbles: true, composed: true,
      }));
    });
    this.shadowRoot.querySelector('button.delete').addEventListener('click', e => {
      e.stopPropagation();
      this.dispatchEvent(new CustomEvent('delete', {
        detail: this._data, bubbles: true, composed: true,
      }));
    });
  }
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function formatRelative(iso) {
  if (!iso) return '';
  const sec = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

customElements.define('af-history-row', AfHistoryRow);
```

- [ ] **Step 4: Implement `af-status-timeline.js`**

```js
/**
 * <af-status-timeline status="queued|extracting|classifying|filing|done|failed">
 *
 * 5-step horizontal timeline. Steps before/at the current status are
 * highlighted in af-blue; steps after are subdued. Failed status puts
 * a red marker at whatever step the pipeline reached.
 */

const STEPS = ['queued', 'extracting', 'classifying', 'filing', 'done'];

const componentSheet = new CSSStyleSheet();
componentSheet.replaceSync(`
  :host { display: block; }
  .timeline {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: var(--af-space-1);
    margin: var(--af-space-3) 0;
  }
  .step {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--af-space-1);
    font: var(--af-small);
    color: var(--af-text-body);
  }
  .step .dot {
    width: 12px; height: 12px;
    border-radius: 50%;
    background: var(--af-border);
  }
  .step .bar {
    width: 100%;
    height: 2px;
    background: var(--af-border);
    margin-top: -7px;
  }
  .step.passed .dot,
  .step.passed .bar { background: var(--af-blue); }
  .step.current .dot {
    background: var(--af-blue);
    box-shadow: 0 0 0 4px rgba(43, 133, 255, .15);
  }
  .step.current { color: var(--af-blue); font-weight: 600; }
  .step.failed .dot { background: var(--af-error); }
  .step.failed { color: var(--af-error); font-weight: 600; }
`);

let tokenSheet = null;
async function getTokensSheet() {
  if (tokenSheet) return tokenSheet;
  const url = chrome.runtime.getURL('lib/design-tokens.css');
  const css = await (await fetch(url)).text();
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(css.replaceAll(':root', ':host'));
  tokenSheet = sheet;
  return sheet;
}

class AfStatusTimeline extends HTMLElement {
  static observedAttributes = ['status', 'failed-at'];

  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.adoptedStyleSheets = [componentSheet];
  }

  async connectedCallback() {
    const sheet = await getTokensSheet();
    this.shadowRoot.adoptedStyleSheets = [sheet, componentSheet];
    this._render();
  }

  attributeChangedCallback() {
    if (this.shadowRoot) this._render();
  }

  _render() {
    const status = this.getAttribute('status') ?? 'queued';
    const failedAt = this.getAttribute('failed-at');  // optional
    const currentIdx = status === 'failed'
      ? STEPS.indexOf(failedAt ?? 'extracting')
      : STEPS.indexOf(status);

    const html = STEPS.map((step, i) => {
      const classes = ['step'];
      classes.push(`data-step-${step}`);
      if (status === 'failed' && i === currentIdx) classes.push('failed');
      else if (i < currentIdx) classes.push('passed');
      else if (i === currentIdx) classes.push('current');
      return `<div class="${classes.join(' ')}" data-step="${step}">
        <div class="dot"></div>
        <div class="label">${step}</div>
      </div>`;
    }).join('');

    this.shadowRoot.innerHTML = `<div class="timeline">${html}</div>`;
  }
}

customElements.define('af-status-timeline', AfStatusTimeline);
```

- [ ] **Step 5: Implement `af-breadcrumb.js`**

```js
/**
 * <af-breadcrumb path="Sources/Socials/Instagram/Recipes">
 *
 * Renders slash-separated path as styled segments.
 */

const componentSheet = new CSSStyleSheet();
componentSheet.replaceSync(`
  :host {
    display: inline-flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--af-space-1);
    font: var(--af-small);
    color: var(--af-text-body);
  }
  .segment {
    color: var(--af-text-body);
  }
  .segment:last-child {
    color: var(--af-navy);
    font-weight: 600;
  }
  .sep {
    color: var(--af-border);
  }
`);

let tokenSheet = null;
async function getTokensSheet() {
  if (tokenSheet) return tokenSheet;
  const url = chrome.runtime.getURL('lib/design-tokens.css');
  const css = await (await fetch(url)).text();
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(css.replaceAll(':root', ':host'));
  tokenSheet = sheet;
  return sheet;
}

class AfBreadcrumb extends HTMLElement {
  static observedAttributes = ['path'];

  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.adoptedStyleSheets = [componentSheet];
  }

  async connectedCallback() {
    const sheet = await getTokensSheet();
    this.shadowRoot.adoptedStyleSheets = [sheet, componentSheet];
    this._render();
  }

  attributeChangedCallback() {
    if (this.shadowRoot) this._render();
  }

  _render() {
    const path = this.getAttribute('path') ?? '';
    const segments = path.split('/').filter(Boolean);
    const html = segments.map((seg, i) => {
      const sep = i < segments.length - 1 ? `<span class="sep">/</span>` : '';
      return `<span class="segment">${escapeHtml(seg)}</span>${sep}`;
    }).join('');
    this.shadowRoot.innerHTML = html;
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

customElements.define('af-breadcrumb', AfBreadcrumb);
```

- [ ] **Step 6: Run — passes**

Expected: 59 / 59 (49 prior + 10 new across the 3 components).

- [ ] **Step 7: Commit**

```sh
git add browser-extension/options/components/af-history-row.js \
        browser-extension/options/components/af-status-timeline.js \
        browser-extension/options/components/af-breadcrumb.js \
        browser-extension/options/components/__tests__/components.test.js
git commit -m "feat(extension): <af-history-row>, <af-status-timeline>, <af-breadcrumb> (phase 6.1)"
```

---

## Task 2: History tab — list, filters, hover actions

**Files:**
- Modify: `browser-extension/options/options.html` (replace History panel placeholder)
- Modify: `browser-extension/options/options.css` (history-specific styles)
- Modify: `browser-extension/options/options.js` (load + render list + filters + actions)

- [ ] **Step 1: Replace the History panel in `options.html`**

Find the existing `<section id="panel-history" ...>` block and replace its entire body:

```html
<section id="panel-history" class="panel" role="tabpanel" data-panel="history" hidden>
  <div class="filter-pills">
    <button class="filter-pill active" data-filter="all">All</button>
    <button class="filter-pill" data-filter="done">Done</button>
    <button class="filter-pill" data-filter="failed">Failed</button>
    <button class="filter-pill" data-filter="in-progress">In progress</button>
  </div>

  <div id="historyList" class="history-list"></div>

  <div id="historyEmpty" class="history-empty" hidden>
    <af-card>
      <p class="hint">No captures yet — try sharing something from a supported site or right-clicking on this page.</p>
    </af-card>
  </div>

  <!-- Detail sub-view; toggled by hash route -->
  <div id="historyDetail" class="history-detail" hidden></div>
</section>
```

- [ ] **Step 2: Append history-specific styles to `options.css`**

```css
/* History tab */

.filter-pills {
  display: flex;
  gap: var(--af-space-1);
  margin-bottom: var(--af-space-3);
}
.filter-pill {
  padding: var(--af-space-1) var(--af-space-3);
  font: var(--af-small);
  font-weight: 500;
  color: var(--af-text-body);
  background: var(--af-surface);
  border: 1px solid var(--af-border);
  border-radius: var(--af-radius-pill);
  cursor: pointer;
  transition: background .15s ease, color .15s ease;
}
.filter-pill:hover { color: var(--af-navy); }
.filter-pill.active {
  background: var(--af-blue);
  color: var(--af-surface);
  border-color: var(--af-blue);
}

.history-list {
  display: flex;
  flex-direction: column;
}

.history-empty {
  margin-top: var(--af-space-3);
  text-align: center;
}

.history-detail {
  background: var(--af-surface);
  border: 1px solid var(--af-border);
  border-radius: var(--af-radius-card);
  padding: var(--af-space-4);
  box-shadow: var(--af-shadow-card);
}
.history-detail .back-btn {
  background: transparent;
  border: none;
  color: var(--af-blue);
  cursor: pointer;
  font: var(--af-small);
  padding: 0;
  margin-bottom: var(--af-space-3);
}
.history-detail .back-btn:hover { text-decoration: underline; }
.history-detail .title {
  font: var(--af-h3);
  margin: 0 0 var(--af-space-1);
  color: var(--af-navy);
}
.history-detail .web-url {
  font: var(--af-small);
  color: var(--af-blue);
  text-decoration: none;
  word-break: break-all;
}
.history-detail .web-url:hover { text-decoration: underline; }
.history-detail .reasoning {
  background: var(--af-bg-soft);
  border-radius: var(--af-radius-button);
  padding: var(--af-space-3);
  margin: var(--af-space-3) 0;
  font: var(--af-small);
  color: var(--af-text-body);
  white-space: pre-wrap;
}
.history-detail .error-block {
  background: #FDE7E7;
  border-radius: var(--af-radius-button);
  padding: var(--af-space-3);
  margin: var(--af-space-3) 0;
  font: var(--af-small);
  color: var(--af-error);
}
.history-detail .detail-actions {
  display: flex;
  gap: var(--af-space-2);
  margin-top: var(--af-space-4);
}
```

- [ ] **Step 3: Extend `options.js`**

Update the imports at the top:

```js
import '../options/components/af-button.js';
import '../options/components/af-input.js';
import '../options/components/af-card.js';
import '../options/components/af-history-row.js';
import '../options/components/af-status-timeline.js';
import '../options/components/af-breadcrumb.js';
import { getConfig, setConfig, getRecentCaptures } from '../lib/storage.js';
import { health, IngestError } from '../lib/api.js';
import { listCaptures, getCapture, retryCapture, deleteCapture } from '../capture/client.js';
```

Update the `VALID_TABS` constant to allow detail sub-routes:

```js
const VALID_TABS = ['settings', 'history', 'cookies'];
```

(Same — no change.) But change `currentTab()` to handle `#history/<id>`:

```js
function currentTab() {
  const hash = window.location.hash.replace('#', '');
  const top = hash.split('/')[0];
  return VALID_TABS.includes(top) ? top : 'settings';
}

function currentDetailId() {
  const hash = window.location.hash.replace('#', '');
  const parts = hash.split('/');
  return parts[0] === 'history' && parts[1] ? parts[1] : null;
}
```

Add to `routeFromHash()`:

```js
function routeFromHash() {
  const target = currentTab();
  for (const tab of $tabs) {
    tab.classList.toggle('active', tab.dataset.tab === target);
  }
  for (const panel of $panels) {
    panel.hidden = panel.dataset.panel !== target;
  }
  if (target === 'history') {
    renderHistoryView();
  }
}
```

Add the history view logic at the end of the file:

```js
const $historyList = document.getElementById('historyList');
const $historyEmpty = document.getElementById('historyEmpty');
const $historyDetail = document.getElementById('historyDetail');

let _historyItems = [];
let _activeFilter = 'all';

document.querySelectorAll('.filter-pill').forEach(pill => {
  pill.addEventListener('click', () => {
    document.querySelectorAll('.filter-pill').forEach(p => p.classList.remove('active'));
    pill.classList.add('active');
    _activeFilter = pill.dataset.filter;
    renderHistoryList();
  });
});

async function renderHistoryView() {
  const detailId = currentDetailId();
  if (detailId) {
    await renderHistoryDetail(detailId);
    return;
  }
  // List view.
  $historyList.hidden = false;
  document.querySelector('.filter-pills').hidden = false;
  $historyDetail.hidden = true;
  await loadHistoryList();
}

async function loadHistoryList() {
  // Seed from cache.
  const cached = await getRecentCaptures();
  if (cached.length) {
    _historyItems = cached;
    renderHistoryList();
  }
  // Refresh from server.
  try {
    const page = await listCaptures({ limit: 50 });
    _historyItems = page?.items ?? [];
    renderHistoryList();
  } catch (e) {
    if (!cached.length) {
      $historyList.innerHTML = '';
      $historyEmpty.hidden = false;
    }
  }
}

function renderHistoryList() {
  const filtered = filterItems(_historyItems, _activeFilter);
  $historyList.innerHTML = '';
  if (filtered.length === 0) {
    $historyEmpty.hidden = false;
    return;
  }
  $historyEmpty.hidden = true;
  for (const item of filtered) {
    const row = document.createElement('af-history-row');
    row.data = item;
    row.addEventListener('open', e => {
      window.location.hash = `#history/${e.detail.capture_id}`;
    });
    row.addEventListener('retry', async e => {
      try {
        await retryCapture(e.detail.capture_id);
        await loadHistoryList();
      } catch (err) { showToast(err?.message ?? 'Retry failed'); }
    });
    row.addEventListener('delete', async e => {
      if (!confirm(`Delete capture "${e.detail.shared_title ?? e.detail.url ?? ''}"?`)) return;
      try {
        await deleteCapture(e.detail.capture_id);
        _historyItems = _historyItems.filter(i => i.capture_id !== e.detail.capture_id);
        renderHistoryList();
      } catch (err) { showToast(err?.message ?? 'Delete failed'); }
    });
    $historyList.appendChild(row);
  }
}

function filterItems(items, filter) {
  if (filter === 'all') return items;
  if (filter === 'done') return items.filter(i => i.status === 'done');
  if (filter === 'failed') return items.filter(i => i.status === 'failed');
  if (filter === 'in-progress') {
    return items.filter(i => ['queued', 'extracting', 'classifying', 'filing'].includes(i.status));
  }
  return items;
}

async function renderHistoryDetail(captureId) {
  $historyList.hidden = true;
  document.querySelector('.filter-pills').hidden = true;
  $historyEmpty.hidden = true;
  $historyDetail.hidden = false;

  $historyDetail.innerHTML = `<p class="hint">Loading…</p>`;
  let detail;
  try {
    detail = await getCapture(captureId);
  } catch (e) {
    $historyDetail.innerHTML = `
      <button class="back-btn" id="back">← Back</button>
      <p class="hint" style="color: var(--af-error)">Couldn't load: ${e?.message ?? e}</p>
    `;
    document.getElementById('back').addEventListener('click', () => { window.location.hash = '#history'; });
    return;
  }

  const title = detail.shared_title ?? detail.url ?? '(untitled)';
  const reasoning = detail.classifier_reasoning
    ? `<div class="reasoning">${escapeText(detail.classifier_reasoning)}</div>`
    : '';
  const errorBlock = detail.status === 'failed' && detail.error
    ? `<div class="error-block">${escapeText(detail.error)}</div>`
    : '';

  $historyDetail.innerHTML = `
    <button class="back-btn" id="back">← Back</button>
    <h2 class="title">${escapeText(title)}</h2>
    <a class="web-url" href="${escapeAttr(detail.web_url ?? '#')}" target="_blank" rel="noopener">${escapeText(detail.web_url ?? '')}</a>
    <af-status-timeline status="${escapeAttr(detail.status)}"></af-status-timeline>
    ${reasoning}
    ${detail.topic_path ? `<af-breadcrumb path="${escapeAttr(detail.topic_path)}"></af-breadcrumb>` : ''}
    ${errorBlock}
    <div class="detail-actions">
      <af-button id="open" variant="primary">Open in AFFiNE</af-button>
      ${detail.status !== 'done' ? `<af-button id="retry" variant="secondary">Retry</af-button>` : ''}
      <af-button id="delete" variant="ghost">Delete</af-button>
    </div>
  `;

  document.getElementById('back').addEventListener('click', () => { window.location.hash = '#history'; });
  document.getElementById('open').addEventListener('click', () => {
    if (detail.web_url) chrome.tabs.create({ url: detail.web_url });
  });
  document.getElementById('retry')?.addEventListener('click', async () => {
    try {
      await retryCapture(captureId);
      await renderHistoryDetail(captureId);
    } catch (e) { showToast(e?.message ?? 'Retry failed'); }
  });
  document.getElementById('delete').addEventListener('click', async () => {
    if (!confirm(`Delete capture "${title}"?`)) return;
    try {
      await deleteCapture(captureId);
      window.location.hash = '#history';
    } catch (e) { showToast(e?.message ?? 'Delete failed'); }
  });
}

function escapeText(s) {
  const div = document.createElement('div');
  div.textContent = String(s ?? '');
  return div.innerHTML;
}
function escapeAttr(s) {
  return String(s ?? '').replace(/"/g, '&quot;').replace(/&/g, '&amp;');
}
```

- [ ] **Step 4: Tests still green (50 + 10 from Task 1 = 60 / 60; no changes here)**

Wait — Task 1 ended at 59 (49 + 10 new). This task adds 0 tests. So expect 59 / 59.

- [ ] **Step 5: Commit**

```sh
git add browser-extension/options/options.html browser-extension/options/options.css browser-extension/options/options.js
git commit -m "feat(extension): History tab with list, filters, hover actions, Detail sub-view (phase 6.2)"
```

---

## Task 3: Manual smoke (USER)

After Tasks 1+2 land:

- [ ] Click "History" tab → list of recent captures appears (seeded from cache instantly, refreshed from server in background).
- [ ] Each row shows: platform icon (YT/IG/X/etc.) + title + topic_path subtle + status badge + relative time.
- [ ] Filter pills: clicking "Done" filters to status=done; "Failed" filters to failed; "In progress" filters to queued/extracting/classifying/filing.
- [ ] Hover a row → Retry icon (only if not `done`) + Delete icon appear.
- [ ] Click Retry on a failed row → status updates within 5s after `/captures/{id}/retry`.
- [ ] Click Delete → native `confirm()` prompt → DELETE → row disappears.
- [ ] Click row body → URL hash updates to `#history/<id>`; Detail view replaces list.
- [ ] Detail view shows: title, web_url link (blue, opens in new tab), 5-step timeline highlighting current step, classifier_reasoning callout (if present), breadcrumb of `topic_path`, Open/Retry/Delete buttons.
- [ ] Click "← Back" → returns to list (URL hash → `#history`).
- [ ] Empty state: clear all captures or filter to a status with none → "No captures yet" card appears.
