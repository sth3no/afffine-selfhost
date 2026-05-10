# Phase 4: Web Components for the AFFiNE design system

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Four reusable Custom Elements (`<af-button>`, `<af-input>`, `<af-status-badge>`, `<af-card>`) that pull all styling from `lib/design-tokens.css`, plus an `lib/icons.js` SVG-string library. Renders identically inside the popup, the options page, and content-script-injected DOM (Phase 8) — Shadow DOM isolation prevents host-page CSS from leaking in.

**Spec:** [`docs/specs/2026-05-10-browser-extension-multitool-design.md`](../specs/2026-05-10-browser-extension-multitool-design.md) §3.1, §5

**Macro plan:** Phase 4 in [`docs/plans/2026-05-10-browser-extension-multitool-macro-plan.md`](2026-05-10-browser-extension-multitool-macro-plan.md)

**Architecture:** Each Custom Element is one ES module that registers itself via `customElements.define()` on import. They use Shadow DOM and `adoptedStyleSheets` so the design tokens flow in without host-page interference. The components are imported by the options page (Phases 5–7) and the in-page pill (Phase 8).

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `browser-extension/lib/icons.js` | Create | SVG string library — exports named SVGs (`linkIcon`, `checkIcon`, `xCircleIcon`, etc.) and `platformIcon(platform)` mapper. All 2 px stroke, `currentColor`. |
| `browser-extension/lib/__tests__/icons.test.js` | Create | Tests `platformIcon` mapping and that exported strings are valid SVG. |
| `browser-extension/options/components/af-button.js` | Create | `<af-button variant="primary\|secondary\|ghost\|icon" size="sm\|md">` |
| `browser-extension/options/components/af-input.js` | Create | `<af-input type="text\|password\|url" paste-button>` w/ paste action |
| `browser-extension/options/components/af-status-badge.js` | Create | `<af-status-badge status="queued\|extracting\|classifying\|filing\|done\|failed">` |
| `browser-extension/options/components/af-card.js` | Create | `<af-card>` styled wrapper w/ shadow + radius + padding |
| `browser-extension/options/components/__tests__/components.test.js` | Create | jsdom tests: registration, attribute reflection, custom events |

---

## Task 1: `lib/icons.js` + tests

**Files:**
- Create: `browser-extension/lib/icons.js`
- Create: `browser-extension/lib/__tests__/icons.test.js`

- [ ] **Step 1: Failing test**

`lib/__tests__/icons.test.js`:

```js
/** @vitest-environment node */
import { describe, it, expect } from 'vitest';
import {
  linkIcon, checkIcon, xCircleIcon, arrowUpRightIcon,
  arrowClockwiseIcon, trashIcon, playRectangleIcon,
  cameraIcon, xLogoIcon, musicNoteIcon, redditIcon,
  platformIcon,
} from '../icons.js';

describe('lib/icons', () => {
  const all = {
    linkIcon, checkIcon, xCircleIcon, arrowUpRightIcon,
    arrowClockwiseIcon, trashIcon, playRectangleIcon,
    cameraIcon, xLogoIcon, musicNoteIcon, redditIcon,
  };

  it('all exports are non-empty SVG strings starting with <svg', () => {
    for (const [name, svg] of Object.entries(all)) {
      expect(svg, name).toMatch(/^<svg[\s>]/);
      expect(svg, name).toMatch(/<\/svg>$/);
    }
  });

  it('uses currentColor for stroke (no hardcoded color)', () => {
    for (const [name, svg] of Object.entries(all)) {
      // Allow fill="none" but not arbitrary stroke colors.
      expect(svg, `${name} should use currentColor`).toMatch(/stroke="currentColor"|fill="currentColor"/);
    }
  });

  it('uses 2px stroke width', () => {
    for (const [name, svg] of Object.entries(all)) {
      // Skip glyph-fill-only icons (no stroke)
      if (svg.includes('stroke="none"') || !svg.includes('stroke=')) continue;
      expect(svg, `${name} stroke-width`).toMatch(/stroke-width="2"/);
    }
  });

  it('platformIcon maps known platforms', () => {
    expect(platformIcon('youtube')).toBe(playRectangleIcon);
    expect(platformIcon('instagram')).toBe(cameraIcon);
    expect(platformIcon('x')).toBe(xLogoIcon);
    expect(platformIcon('twitter')).toBe(xLogoIcon);
    expect(platformIcon('tiktok')).toBe(musicNoteIcon);
    expect(platformIcon('reddit')).toBe(redditIcon);
  });

  it('platformIcon falls back to linkIcon for unknown', () => {
    expect(platformIcon('article')).toBe(linkIcon);
    expect(platformIcon('unknown-platform')).toBe(linkIcon);
    expect(platformIcon(null)).toBe(linkIcon);
  });
});
```

- [ ] **Step 2: Run — fails**

```sh
cd C:/Users/PC/Projects/ToEverything/portainer-stack/browser-extension
npm test -- lib/__tests__/icons.test.js
```

Expected: FAIL — Cannot find module.

- [ ] **Step 3: Implement `lib/icons.js`**

```js
/**
 * SVG string library. All icons:
 *   - 2px stroke
 *   - currentColor (so they inherit from the surrounding text color)
 *   - 24x24 viewBox normalized
 *   - line-cap round / line-join round
 *
 * Used by Web Components (Phase 4) and history rows (Phase 6).
 *
 * Design references the AFFiNE spec §5: linear thin-stroke icon style.
 */

const SVG_ATTRS = 'xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"';

export const linkIcon = `<svg ${SVG_ATTRS}><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>`;

export const checkIcon = `<svg ${SVG_ATTRS}><polyline points="20 6 9 17 4 12"/></svg>`;

export const xCircleIcon = `<svg ${SVG_ATTRS}><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>`;

export const arrowUpRightIcon = `<svg ${SVG_ATTRS}><line x1="7" y1="17" x2="17" y2="7"/><polyline points="7 7 17 7 17 17"/></svg>`;

export const arrowClockwiseIcon = `<svg ${SVG_ATTRS}><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>`;

export const trashIcon = `<svg ${SVG_ATTRS}><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>`;

export const playRectangleIcon = `<svg ${SVG_ATTRS}><rect x="2" y="4" width="20" height="16" rx="2" ry="2"/><polygon points="10 9 16 12 10 15"/></svg>`;

export const cameraIcon = `<svg ${SVG_ATTRS}><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>`;

export const xLogoIcon = `<svg ${SVG_ATTRS}><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;

export const musicNoteIcon = `<svg ${SVG_ATTRS}><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>`;

export const redditIcon = `<svg ${SVG_ATTRS}><circle cx="12" cy="12" r="10"/><path d="M8 14a4 4 0 0 0 8 0"/><circle cx="9" cy="11" r="1" fill="currentColor"/><circle cx="15" cy="11" r="1" fill="currentColor"/></svg>`;

const PLATFORM_MAP = {
  youtube: playRectangleIcon,
  instagram: cameraIcon,
  x: xLogoIcon,
  twitter: xLogoIcon,
  tiktok: musicNoteIcon,
  reddit: redditIcon,
};

/**
 * @param {string|null|undefined} platform
 * @returns {string} SVG string; falls back to linkIcon for unknown / null.
 */
export function platformIcon(platform) {
  return PLATFORM_MAP[platform] ?? linkIcon;
}
```

- [ ] **Step 4: Run — passes**

```sh
npm test -- lib/__tests__/icons.test.js
```

Expected: 5 / 5 passing (some assertions iterate per-icon).

- [ ] **Step 5: Commit**

```sh
git add browser-extension/lib/icons.js browser-extension/lib/__tests__/icons.test.js
git commit -m "feat(extension): lib/icons SVG library + platform mapping (phase 4.1)"
```

---

## Task 2: `<af-button>` + tests

**Files:**
- Create: `browser-extension/options/components/af-button.js`
- Create: `browser-extension/options/components/__tests__/components.test.js` (this file holds tests for ALL 4 components — start it here, extend in Tasks 3/4/5)

- [ ] **Step 1: Failing test**

`options/components/__tests__/components.test.js`:

```js
/** @vitest-environment jsdom */
import { describe, it, expect, beforeAll } from 'vitest';
import '../af-button.js';

describe('<af-button>', () => {
  it('registers the custom element', () => {
    expect(customElements.get('af-button')).toBeTypeOf('function');
  });

  it('renders a button inside Shadow DOM', () => {
    const el = document.createElement('af-button');
    el.textContent = 'Save';
    document.body.appendChild(el);
    const btn = el.shadowRoot?.querySelector('button');
    expect(btn).toBeTruthy();
  });

  it('reflects variant=primary by default', () => {
    const el = document.createElement('af-button');
    document.body.appendChild(el);
    const btn = el.shadowRoot.querySelector('button');
    expect(btn.classList.contains('primary')).toBe(true);
  });

  it('reflects variant=secondary attribute', () => {
    const el = document.createElement('af-button');
    el.setAttribute('variant', 'secondary');
    document.body.appendChild(el);
    const btn = el.shadowRoot.querySelector('button');
    expect(btn.classList.contains('secondary')).toBe(true);
    expect(btn.classList.contains('primary')).toBe(false);
  });

  it('disabled attribute disables the inner button', () => {
    const el = document.createElement('af-button');
    el.setAttribute('disabled', '');
    document.body.appendChild(el);
    expect(el.shadowRoot.querySelector('button').disabled).toBe(true);
  });

  it('forwards click events from inner button', async () => {
    const el = document.createElement('af-button');
    document.body.appendChild(el);
    let clicks = 0;
    el.addEventListener('click', () => clicks++);
    el.shadowRoot.querySelector('button').click();
    expect(clicks).toBe(1);
  });
});
```

- [ ] **Step 2: Run — fails**

```sh
npm test -- options/components/__tests__/components.test.js
```

Expected: FAIL — Cannot find module.

- [ ] **Step 3: Implement `af-button.js`**

```js
/**
 * <af-button variant="primary|secondary|ghost|icon" size="sm|md" disabled?>
 *
 * AFFiNE-styled button. Uses Shadow DOM to isolate from host-page CSS;
 * design tokens flow in via adoptedStyleSheets loaded from
 * lib/design-tokens.css.
 *
 * Click events bubble naturally (composed: true on inner button click).
 *
 * Slot: button label (text or inline SVG).
 */

let tokenSheet = null;
async function getTokensSheet() {
  if (tokenSheet) return tokenSheet;
  const url = chrome.runtime.getURL('lib/design-tokens.css');
  const css = await (await fetch(url)).text();
  // Re-target :root to :host so the variables apply inside Shadow DOM.
  const shadowized = css.replaceAll(':root', ':host');
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(shadowized);
  tokenSheet = sheet;
  return sheet;
}

const componentSheet = new CSSStyleSheet();
componentSheet.replaceSync(`
  :host { display: inline-block; }
  button {
    font-family: var(--af-font);
    font-size: 14px;
    font-weight: 600;
    border-radius: var(--af-radius-button);
    border: 1px solid transparent;
    cursor: pointer;
    padding: var(--af-space-2) var(--af-space-3);
    transition: transform .15s ease, filter .15s ease, background .15s ease;
  }
  button[disabled] { opacity: .5; cursor: default; }
  button:not([disabled]):hover { transform: translateY(-1px); }
  button:not([disabled]):active { filter: brightness(0.92); transform: none; }

  button.primary {
    background: var(--af-blue);
    color: var(--af-surface);
  }
  button.secondary {
    background: var(--af-surface);
    color: var(--af-blue);
    border-color: var(--af-blue);
  }
  button.ghost {
    background: transparent;
    color: var(--af-blue);
  }
  button.icon {
    background: transparent;
    color: var(--af-text-body);
    padding: var(--af-space-1);
    border-radius: var(--af-radius-pill);
    line-height: 0;
  }
  button.icon:hover { background: var(--af-bg-soft); color: var(--af-blue); }

  button.sm { padding: var(--af-space-1) var(--af-space-2); font-size: 13px; }
`);

class AfButton extends HTMLElement {
  static observedAttributes = ['variant', 'size', 'disabled'];

  constructor() {
    super();
    const root = this.attachShadow({ mode: 'open' });
    root.innerHTML = `<button class="primary"><slot></slot></button>`;
    root.adoptedStyleSheets = [componentSheet];
  }

  async connectedCallback() {
    const sheet = await getTokensSheet();
    this.shadowRoot.adoptedStyleSheets = [sheet, componentSheet];
    this._sync();
  }

  attributeChangedCallback() {
    if (this.shadowRoot) this._sync();
  }

  _sync() {
    const btn = this.shadowRoot.querySelector('button');
    btn.className = '';
    btn.classList.add(this.getAttribute('variant') || 'primary');
    if (this.hasAttribute('size')) btn.classList.add(this.getAttribute('size'));
    btn.disabled = this.hasAttribute('disabled');
  }
}

customElements.define('af-button', AfButton);
```

- [ ] **Step 4: Test stub for `chrome.runtime.getURL` and `fetch`**

The component fetches `lib/design-tokens.css` via `chrome.runtime.getURL`. In jsdom tests, neither `chrome.runtime.getURL` nor a real `fetch` is hooked up — we need to stub.

Add to `test/setup.js` (after the existing `chrome` stub):

```js
chrome.runtime.getURL = vi.fn((path) => `chrome-extension://test/${path}`);

// jsdom 25 has fetch. Stub the design-tokens.css response so components
// can load without network. Other tests can override per-test if needed.
const ORIGINAL_FETCH = globalThis.fetch;
globalThis.fetch = vi.fn(async (url) => {
  if (typeof url === 'string' && url.endsWith('/lib/design-tokens.css')) {
    return new Response(':root { --af-blue: #2B85FF; }', {
      status: 200,
      headers: { 'content-type': 'text/css' },
    });
  }
  if (ORIGINAL_FETCH) return ORIGINAL_FETCH(url);
  throw new Error(`unstubbed fetch: ${url}`);
});
```

(Existing `lib/api.test.js` overrides `globalThis.fetch` per-test inside `beforeEach`, so this stub only matters for jsdom-environment component tests.)

- [ ] **Step 5: Run — passes**

```sh
npm test -- options/components/__tests__/components.test.js
```

Expected: 6 / 6 passing.

- [ ] **Step 6: Commit**

```sh
git add browser-extension/options/components/af-button.js \
        browser-extension/options/components/__tests__/components.test.js \
        browser-extension/test/setup.js
git commit -m "feat(extension): <af-button> Web Component + chrome.runtime.getURL stub (phase 4.2)"
```

---

## Task 3: `<af-input>` + tests

**Files:**
- Create: `browser-extension/options/components/af-input.js`
- Modify: `browser-extension/options/components/__tests__/components.test.js` (append tests)

- [ ] **Step 1: Append failing tests**

Add to `components.test.js` after the existing `describe('<af-button>')`:

```js
import '../af-input.js';

describe('<af-input>', () => {
  it('registers the custom element', () => {
    expect(customElements.get('af-input')).toBeTypeOf('function');
  });

  it('renders an input inside Shadow DOM', () => {
    const el = document.createElement('af-input');
    document.body.appendChild(el);
    expect(el.shadowRoot.querySelector('input')).toBeTruthy();
  });

  it('reflects type=password attribute', () => {
    const el = document.createElement('af-input');
    el.setAttribute('type', 'password');
    document.body.appendChild(el);
    expect(el.shadowRoot.querySelector('input').type).toBe('password');
  });

  it('value getter/setter round-trip', () => {
    const el = document.createElement('af-input');
    document.body.appendChild(el);
    el.value = 'hello';
    expect(el.value).toBe('hello');
    expect(el.shadowRoot.querySelector('input').value).toBe('hello');
  });

  it('paste-button attribute renders a paste button', () => {
    const el = document.createElement('af-input');
    el.setAttribute('paste-button', '');
    document.body.appendChild(el);
    expect(el.shadowRoot.querySelector('button.paste')).toBeTruthy();
  });

  it('emits change event when input changes', () => {
    const el = document.createElement('af-input');
    document.body.appendChild(el);
    let received = null;
    el.addEventListener('change', e => { received = e.target.value; });
    const inner = el.shadowRoot.querySelector('input');
    inner.value = 'new';
    inner.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
    expect(received).toBe('new');
  });
});
```

- [ ] **Step 2: Run — failing**

```sh
npm test -- options/components/__tests__/components.test.js
```

Expected: only `<af-button>` tests pass; new ones fail with "Cannot find module".

- [ ] **Step 3: Implement `af-input.js`**

```js
/**
 * <af-input type="text|password|url" value="…" paste-button?>
 *
 * AFFiNE-styled input. Optional paste button uses navigator.clipboard.readText.
 *
 * Events: 'change' bubbles when the inner input changes (composed). Use
 * `el.value` to read/write the current value.
 */

const componentSheet = new CSSStyleSheet();
componentSheet.replaceSync(`
  :host { display: block; }
  .wrap {
    display: flex; align-items: stretch;
    border: 1px solid var(--af-border);
    border-radius: var(--af-radius-button);
    background: var(--af-surface);
    overflow: hidden;
  }
  .wrap:focus-within { border-color: var(--af-blue); }
  input {
    flex: 1;
    border: none;
    outline: none;
    padding: var(--af-space-2) var(--af-space-3);
    font: var(--af-body);
    color: var(--af-navy);
    background: transparent;
  }
  input::placeholder { color: var(--af-text-body); }
  button.paste {
    border: none;
    border-left: 1px solid var(--af-border);
    background: var(--af-gray-50);
    color: var(--af-blue);
    padding: 0 var(--af-space-3);
    font: var(--af-small);
    cursor: pointer;
  }
  button.paste:hover { background: var(--af-bg-soft); }
`);

// Shared loader from af-button.js — re-import the helper to keep one source.
let tokenSheet = null;
async function getTokensSheet() {
  if (tokenSheet) return tokenSheet;
  const url = chrome.runtime.getURL('lib/design-tokens.css');
  const css = await (await fetch(url)).text();
  const shadowized = css.replaceAll(':root', ':host');
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(shadowized);
  tokenSheet = sheet;
  return sheet;
}

class AfInput extends HTMLElement {
  static observedAttributes = ['type', 'value', 'placeholder', 'paste-button'];

  constructor() {
    super();
    const root = this.attachShadow({ mode: 'open' });
    root.innerHTML = `
      <div class="wrap">
        <input type="text">
      </div>
    `;
    root.adoptedStyleSheets = [componentSheet];
  }

  async connectedCallback() {
    const sheet = await getTokensSheet();
    this.shadowRoot.adoptedStyleSheets = [sheet, componentSheet];
    this._sync();
    this._wire();
  }

  attributeChangedCallback() {
    if (this.shadowRoot) this._sync();
  }

  get value() { return this.shadowRoot.querySelector('input')?.value ?? ''; }
  set value(v) {
    const i = this.shadowRoot.querySelector('input');
    if (i) i.value = v ?? '';
  }

  _sync() {
    const wrap = this.shadowRoot.querySelector('.wrap');
    const input = this.shadowRoot.querySelector('input');
    input.type = this.getAttribute('type') || 'text';
    if (this.hasAttribute('placeholder')) input.placeholder = this.getAttribute('placeholder');
    if (this.hasAttribute('value')) input.value = this.getAttribute('value');
    // Paste button toggle.
    const want = this.hasAttribute('paste-button');
    const exists = wrap.querySelector('button.paste');
    if (want && !exists) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'paste';
      btn.textContent = 'Paste';
      btn.addEventListener('click', async () => {
        try {
          input.value = await navigator.clipboard.readText();
          input.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
        } catch { /* ignore — user can still type */ }
      });
      wrap.appendChild(btn);
    } else if (!want && exists) {
      exists.remove();
    }
  }

  _wire() {
    // Already inserted; events bubble out via the inner input's
    // composed:true reflexivity isn't automatic — relay manually.
    const input = this.shadowRoot.querySelector('input');
    input.addEventListener('input', () => {
      this.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
    });
    input.addEventListener('change', () => {
      this.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
    });
  }
}

customElements.define('af-input', AfInput);
```

- [ ] **Step 4: Run — passes**

Expected: 12 / 12 passing across `<af-button>` + `<af-input>`.

- [ ] **Step 5: Commit**

```sh
git add browser-extension/options/components/af-input.js \
        browser-extension/options/components/__tests__/components.test.js
git commit -m "feat(extension): <af-input> Web Component (phase 4.3)"
```

---

## Task 4: `<af-status-badge>` + tests

**Files:**
- Create: `browser-extension/options/components/af-status-badge.js`
- Modify: `components.test.js` (append)

- [ ] **Step 1: Append tests**

```js
import '../af-status-badge.js';

describe('<af-status-badge>', () => {
  it('registers', () => {
    expect(customElements.get('af-status-badge')).toBeTypeOf('function');
  });

  it('renders done with green check', () => {
    const el = document.createElement('af-status-badge');
    el.setAttribute('status', 'done');
    document.body.appendChild(el);
    const root = el.shadowRoot;
    expect(root.querySelector('.done')).toBeTruthy();
    expect(root.innerHTML).toContain('<polyline');  // checkIcon
  });

  it('renders failed with red x', () => {
    const el = document.createElement('af-status-badge');
    el.setAttribute('status', 'failed');
    document.body.appendChild(el);
    expect(el.shadowRoot.querySelector('.failed')).toBeTruthy();
  });

  it('renders queued/extracting/classifying/filing as in-progress', () => {
    for (const s of ['queued', 'extracting', 'classifying', 'filing']) {
      const el = document.createElement('af-status-badge');
      el.setAttribute('status', s);
      document.body.appendChild(el);
      expect(el.shadowRoot.querySelector('.in-progress'), s).toBeTruthy();
    }
  });

  it('renders fallback for unknown status', () => {
    const el = document.createElement('af-status-badge');
    el.setAttribute('status', 'mystery');
    document.body.appendChild(el);
    expect(el.shadowRoot.querySelector('.unknown')).toBeTruthy();
  });
});
```

- [ ] **Step 2: Implement `af-status-badge.js`**

```js
/**
 * <af-status-badge status="queued|extracting|classifying|filing|done|failed|deleted">
 *
 * Compact icon-style badge:
 *   - done: green check
 *   - failed: red X-circle
 *   - queued/extracting/classifying/filing: blue spinner-dot (animated)
 *   - deleted/unknown: gray dot
 */
import { checkIcon, xCircleIcon } from '../../lib/icons.js';

const componentSheet = new CSSStyleSheet();
componentSheet.replaceSync(`
  :host { display: inline-flex; align-items: center; gap: var(--af-space-1); font: var(--af-small); }
  .badge {
    display: inline-flex; align-items: center; justify-content: center;
    width: 18px; height: 18px;
  }
  .done { color: var(--af-success); }
  .failed { color: var(--af-error); }
  .in-progress { color: var(--af-blue); }
  .in-progress::before {
    content: '';
    width: 10px; height: 10px;
    border-radius: 50%;
    background: var(--af-blue);
    animation: pulse 1s ease-in-out infinite;
  }
  .unknown {
    color: var(--af-text-body);
  }
  .unknown::before {
    content: '';
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--af-text-body);
    opacity: .5;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: .5; transform: scale(0.85); }
  }
  .label { color: var(--af-text-body); }
`);

let tokenSheet = null;
async function getTokensSheet() {
  if (tokenSheet) return tokenSheet;
  const url = chrome.runtime.getURL('lib/design-tokens.css');
  const css = await (await fetch(url)).text();
  const shadowized = css.replaceAll(':root', ':host');
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(shadowized);
  tokenSheet = sheet;
  return sheet;
}

const STATUS_VARIANTS = {
  done: { className: 'done', body: checkIcon },
  failed: { className: 'failed', body: xCircleIcon },
  queued: { className: 'in-progress', body: '' },
  extracting: { className: 'in-progress', body: '' },
  classifying: { className: 'in-progress', body: '' },
  filing: { className: 'in-progress', body: '' },
};

class AfStatusBadge extends HTMLElement {
  static observedAttributes = ['status', 'show-label'];

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
    const status = this.getAttribute('status') ?? 'unknown';
    const variant = STATUS_VARIANTS[status] ?? { className: 'unknown', body: '' };
    const label = this.hasAttribute('show-label')
      ? `<span class="label">${escapeHtml(status)}</span>`
      : '';
    this.shadowRoot.innerHTML = `<span class="badge ${variant.className}">${variant.body}</span>${label}`;
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

customElements.define('af-status-badge', AfStatusBadge);
```

- [ ] **Step 3: Run + commit**

```sh
npm test -- options/components/__tests__/components.test.js
```

Expected: 17 / 17 passing.

```sh
git add browser-extension/options/components/af-status-badge.js \
        browser-extension/options/components/__tests__/components.test.js
git commit -m "feat(extension): <af-status-badge> Web Component (phase 4.4)"
```

---

## Task 5: `<af-card>` + tests

**Files:**
- Create: `browser-extension/options/components/af-card.js`
- Modify: `components.test.js` (append final block)

- [ ] **Step 1: Append tests**

```js
import '../af-card.js';

describe('<af-card>', () => {
  it('registers', () => {
    expect(customElements.get('af-card')).toBeTypeOf('function');
  });

  it('slots arbitrary content', () => {
    const el = document.createElement('af-card');
    el.innerHTML = '<p>hello</p>';
    document.body.appendChild(el);
    expect(el.querySelector('p')?.textContent).toBe('hello');
  });

  it('has a slot inside the shadow root', () => {
    const el = document.createElement('af-card');
    document.body.appendChild(el);
    expect(el.shadowRoot.querySelector('slot')).toBeTruthy();
  });
});
```

- [ ] **Step 2: Implement `af-card.js`**

```js
/**
 * <af-card>
 *
 * Plain styled wrapper: rounded corners, subtle shadow, 24 px padding.
 * Useful for grouping related controls (Settings tab, History rows).
 */

const componentSheet = new CSSStyleSheet();
componentSheet.replaceSync(`
  :host {
    display: block;
    background: var(--af-surface);
    border: 1px solid var(--af-border);
    border-radius: var(--af-radius-card);
    box-shadow: var(--af-shadow-card);
    padding: var(--af-space-4);
  }
`);

let tokenSheet = null;
async function getTokensSheet() {
  if (tokenSheet) return tokenSheet;
  const url = chrome.runtime.getURL('lib/design-tokens.css');
  const css = await (await fetch(url)).text();
  const shadowized = css.replaceAll(':root', ':host');
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(shadowized);
  tokenSheet = sheet;
  return sheet;
}

class AfCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `<slot></slot>`;
    this.shadowRoot.adoptedStyleSheets = [componentSheet];
  }

  async connectedCallback() {
    const sheet = await getTokensSheet();
    this.shadowRoot.adoptedStyleSheets = [sheet, componentSheet];
  }
}

customElements.define('af-card', AfCard);
```

- [ ] **Step 3: Run + commit**

```sh
npm test
```

Expected: 20 / 20 passing across all components + prior tests (24 + 5 icons + 6 button + 6 input + 5 status-badge + 3 card = 49? recount:
- Phase 1+2+3 carryover: 24
- Phase 4.1 icons: 5
- Phase 4.2 button: 6
- Phase 4.3 input: 6
- Phase 4.4 status-badge: 5
- Phase 4.5 card: 3
- Total: 49

If actual is 49, all good. If different, recount and confirm no regressions.

```sh
git add browser-extension/options/components/af-card.js \
        browser-extension/options/components/__tests__/components.test.js
git commit -m "feat(extension): <af-card> Web Component (phase 4.5)"
```
