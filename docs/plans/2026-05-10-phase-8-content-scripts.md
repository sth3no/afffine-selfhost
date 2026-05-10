# Phase 8: Content scripts — `<af-pill>` + 5 site adapters

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Inject an AFFiNE-styled "Save to AFFiNE" pill into 5 supported sites (YouTube, Instagram, Twitter/X, TikTok, Reddit). One click per item → captures the canonical per-item URL via the same `performCapture` flow popup + context-menu use. Shadow-DOM-encapsulated so host CSS can't break it.

**Spec:** [`docs/specs/2026-05-10-browser-extension-multitool-design.md`](../specs/2026-05-10-browser-extension-multitool-design.md) §4.4

**Macro plan:** Phase 8 in [`docs/plans/2026-05-10-browser-extension-multitool-macro-plan.md`](2026-05-10-browser-extension-multitool-macro-plan.md)

**Architecture:** Each site script (a) imports the shared `<af-pill>` component + the site's canonicalizer + a `dispatchCapture` helper, (b) sets up a `MutationObserver` to find anchor elements as they appear (SPA infinite scroll), (c) inserts a pill into each anchor, (d) on click, computes the canonical URL and posts a `{type:'capture'}` message to the background.

The pill is Shadow-DOM-rooted on the page — its styles can't leak out, host CSS can't leak in.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `browser-extension/content/_shared/pill.js` | Create | `<af-pill data-url="..." data-source="...">` — clickable badge that dispatches a capture message and animates feedback. |
| `browser-extension/content/_shared/canonicalizers.js` | Create | 5 pure functions: `youtubeUrl`, `instagramUrl`, `twitterUrl`, `tiktokUrl`, `redditUrl`. Each takes a raw URL or path → canonical capture URL or null. |
| `browser-extension/content/_shared/dispatch.js` | Create | `dispatchCapture({url, source_app, shared_title?})` — single shared `chrome.runtime.sendMessage` wrapper used by all 5 site scripts. |
| `browser-extension/content/__tests__/canonicalizers.test.js` | Create | 25+ assertions: 5 sites × 5+ URL variants. |
| `browser-extension/content/youtube.js` | Create | Anchor: `ytd-watch-metadata #actions`. Captures watch URL. |
| `browser-extension/content/instagram.js` | Create | Anchor: per-post `article[role=presentation]`. Captures `/p/<id>/` or `/reel/<id>/`. |
| `browser-extension/content/twitter.js` | Create | Anchor: per-tweet `article[data-testid=tweet]`. Captures `/<user>/status/<id>`. |
| `browser-extension/content/tiktok.js` | Create | Anchor: per-FYP card. Captures `@<user>/video/<id>`. |
| `browser-extension/content/reddit.js` | Create | Anchor: `shreddit-post` or fallback. Captures permalink. |
| `browser-extension/manifest.json` | Modify | Add `content_scripts` declarations for the 5 sites. |

---

## Task 1: Foundational — `<af-pill>` + canonicalizers + dispatch + manifest

This task lays everything down except the per-site DOM-walking. After this task, the 5 site scripts in Tasks 2/3 are short.

**Files:**
- Create: `browser-extension/content/_shared/pill.js`
- Create: `browser-extension/content/_shared/canonicalizers.js`
- Create: `browser-extension/content/_shared/dispatch.js`
- Create: `browser-extension/content/__tests__/canonicalizers.test.js`
- Modify: `browser-extension/manifest.json`

- [ ] **Step 1: Failing canonicalizers test**

`content/__tests__/canonicalizers.test.js`:

```js
/** @vitest-environment node */
import { describe, it, expect } from 'vitest';
import {
  youtubeUrl, instagramUrl, twitterUrl, tiktokUrl, redditUrl,
} from '../_shared/canonicalizers.js';

describe('canonicalizers/youtubeUrl', () => {
  it('strips playlist + index params, keeps v=', () => {
    expect(youtubeUrl('https://www.youtube.com/watch?v=abc&list=foo&index=2'))
      .toBe('https://www.youtube.com/watch?v=abc');
  });
  it('strips timestamp t=', () => {
    expect(youtubeUrl('https://www.youtube.com/watch?v=abc&t=42'))
      .toBe('https://www.youtube.com/watch?v=abc');
  });
  it('handles youtu.be short URL', () => {
    expect(youtubeUrl('https://youtu.be/abc?si=xyz'))
      .toBe('https://www.youtube.com/watch?v=abc');
  });
  it('handles m.youtube.com', () => {
    expect(youtubeUrl('https://m.youtube.com/watch?v=abc'))
      .toBe('https://www.youtube.com/watch?v=abc');
  });
  it('returns null on no v=', () => {
    expect(youtubeUrl('https://www.youtube.com/feed/subscriptions')).toBeNull();
  });
});

describe('canonicalizers/instagramUrl', () => {
  it('post permalink', () => {
    expect(instagramUrl('https://www.instagram.com/p/AbC123/'))
      .toBe('https://www.instagram.com/p/AbC123/');
  });
  it('reel permalink', () => {
    expect(instagramUrl('https://www.instagram.com/reel/AbC123/'))
      .toBe('https://www.instagram.com/reel/AbC123/');
  });
  it('strips query string + utm', () => {
    expect(instagramUrl('https://www.instagram.com/p/AbC123/?utm_source=ig_web'))
      .toBe('https://www.instagram.com/p/AbC123/');
  });
  it('returns null for profile root', () => {
    expect(instagramUrl('https://www.instagram.com/some_user/')).toBeNull();
  });
  it('returns null for stories', () => {
    expect(instagramUrl('https://www.instagram.com/stories/some_user/123/')).toBeNull();
  });
});

describe('canonicalizers/twitterUrl', () => {
  it('extracts user + status from x.com', () => {
    expect(twitterUrl('https://x.com/elonmusk/status/12345'))
      .toBe('https://x.com/elonmusk/status/12345');
  });
  it('normalizes twitter.com → x.com', () => {
    expect(twitterUrl('https://twitter.com/jack/status/9999'))
      .toBe('https://x.com/jack/status/9999');
  });
  it('strips trailing /photo/1 or other paths', () => {
    expect(twitterUrl('https://x.com/foo/status/42/photo/1'))
      .toBe('https://x.com/foo/status/42');
  });
  it('strips query', () => {
    expect(twitterUrl('https://x.com/foo/status/42?s=20')).toBe('https://x.com/foo/status/42');
  });
  it('null on profile pages', () => {
    expect(twitterUrl('https://x.com/foo')).toBeNull();
  });
});

describe('canonicalizers/tiktokUrl', () => {
  it('extracts @user/video/id', () => {
    expect(tiktokUrl('https://www.tiktok.com/@user/video/123?some=q'))
      .toBe('https://www.tiktok.com/@user/video/123');
  });
  it('handles m.tiktok.com', () => {
    expect(tiktokUrl('https://m.tiktok.com/@user/video/123'))
      .toBe('https://www.tiktok.com/@user/video/123');
  });
  it('null on FYP feed', () => {
    expect(tiktokUrl('https://www.tiktok.com/foryou')).toBeNull();
  });
  it('null on user profile root', () => {
    expect(tiktokUrl('https://www.tiktok.com/@user')).toBeNull();
  });
  it('strips trailing slash', () => {
    expect(tiktokUrl('https://www.tiktok.com/@user/video/123/'))
      .toBe('https://www.tiktok.com/@user/video/123');
  });
});

describe('canonicalizers/redditUrl', () => {
  it('extracts /r/sub/comments/id/slug/', () => {
    expect(redditUrl('https://www.reddit.com/r/programming/comments/abc/some_post/'))
      .toBe('https://www.reddit.com/r/programming/comments/abc/some_post/');
  });
  it('handles old.reddit.com → www', () => {
    expect(redditUrl('https://old.reddit.com/r/programming/comments/abc/some_post/'))
      .toBe('https://www.reddit.com/r/programming/comments/abc/some_post/');
  });
  it('handles short comment links', () => {
    expect(redditUrl('https://www.reddit.com/r/programming/comments/abc/'))
      .toBe('https://www.reddit.com/r/programming/comments/abc/');
  });
  it('strips utm', () => {
    expect(redditUrl('https://www.reddit.com/r/programming/comments/abc/some_post/?utm_source=share'))
      .toBe('https://www.reddit.com/r/programming/comments/abc/some_post/');
  });
  it('null on subreddit root', () => {
    expect(redditUrl('https://www.reddit.com/r/programming/')).toBeNull();
  });
});
```

- [ ] **Step 2: Run — fails (Cannot find module)**

```sh
cd C:/Users/PC/Projects/ToEverything/portainer-stack/browser-extension
npm test -- content/__tests__/canonicalizers.test.js
```

- [ ] **Step 3: Implement `content/_shared/canonicalizers.js`**

```js
/**
 * Per-site URL canonicalizers. Pure functions: input URL string →
 * canonical capture URL (string) or null (no per-item URL detected).
 *
 * Used by content/<site>.js scripts to compute the URL to send to /capture
 * when the user clicks the in-page pill.
 */

export function youtubeUrl(rawUrl) {
  let u;
  try { u = new URL(rawUrl); } catch { return null; }
  // Short URL: youtu.be/<id>
  if (u.hostname === 'youtu.be') {
    const id = u.pathname.slice(1).split('/')[0];
    if (!id) return null;
    return `https://www.youtube.com/watch?v=${id}`;
  }
  // youtube.com / m.youtube.com / www.youtube.com
  if (!/(?:^|\.)youtube\.com$/.test(u.hostname)) return null;
  if (u.pathname !== '/watch') return null;
  const v = u.searchParams.get('v');
  if (!v) return null;
  return `https://www.youtube.com/watch?v=${v}`;
}

export function instagramUrl(rawUrl) {
  let u;
  try { u = new URL(rawUrl); } catch { return null; }
  if (!/(?:^|\.)instagram\.com$/.test(u.hostname)) return null;
  // Match /p/<id>/ or /reel/<id>/  (NOT /stories/...)
  const m = u.pathname.match(/^\/(p|reel)\/([^/]+)\/?/);
  if (!m) return null;
  return `https://www.instagram.com/${m[1]}/${m[2]}/`;
}

export function twitterUrl(rawUrl) {
  let u;
  try { u = new URL(rawUrl); } catch { return null; }
  if (u.hostname !== 'x.com' && u.hostname !== 'twitter.com'
      && u.hostname !== 'www.x.com' && u.hostname !== 'www.twitter.com') {
    return null;
  }
  // Match /<user>/status/<id> with optional trailing path (e.g. /photo/1).
  const m = u.pathname.match(/^\/([^/]+)\/status\/(\d+)/);
  if (!m) return null;
  return `https://x.com/${m[1]}/status/${m[2]}`;
}

export function tiktokUrl(rawUrl) {
  let u;
  try { u = new URL(rawUrl); } catch { return null; }
  if (!/(?:^|\.)tiktok\.com$/.test(u.hostname)) return null;
  const m = u.pathname.match(/^\/(@[^/]+)\/video\/(\d+)\/?$/);
  if (!m) return null;
  return `https://www.tiktok.com/${m[1]}/video/${m[2]}`;
}

export function redditUrl(rawUrl) {
  let u;
  try { u = new URL(rawUrl); } catch { return null; }
  if (!/(?:^|\.)reddit\.com$/.test(u.hostname)) return null;
  // /r/<sub>/comments/<id>[/<slug>][/]
  const m = u.pathname.match(/^\/r\/([^/]+)\/comments\/([^/]+)(\/[^/]+)?\/?/);
  if (!m) return null;
  const slug = m[3] ?? '';
  return `https://www.reddit.com/r/${m[1]}/comments/${m[2]}${slug}/`;
}
```

- [ ] **Step 4: Run — passes**

Expected: 25 / 25 tests pass.

- [ ] **Step 5: Implement `content/_shared/pill.js`**

```js
/**
 * <af-pill data-url="..." data-source="...">
 *
 * Floating in-page pill. Shadow-DOM rooted so host CSS can't break it.
 * On click, dispatches a capture message and animates a checkmark on success.
 *
 * The dispatcher is imported lazily so the pill module stays small and
 * portable.
 */
import { dispatchCapture } from './dispatch.js';

const sheet = new CSSStyleSheet();
sheet.replaceSync(`
  :host {
    display: inline-block;
    font-family: -apple-system, system-ui, 'Inter', 'Segoe UI', sans-serif;
    font-size: 12px;
    font-weight: 600;
  }
  button {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    background: #2B85FF;
    color: #fff;
    border: none;
    border-radius: 999px;
    cursor: pointer;
    box-shadow: 0 1px 2px rgba(0, 26, 63, .2);
    transition: background .15s ease, transform .15s ease;
  }
  button:hover { background: #1f6fdc; transform: translateY(-1px); }
  button:active { transform: none; }
  button[disabled] { opacity: .7; cursor: default; transform: none; }
  button.ok { background: #4CAF50; }
  button.err { background: #FF4D4F; }
  .icon { line-height: 0; }
`);

const SAVE_SVG = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="19 21 12 16 5 21 5 5 19 5 19 21"/></svg>`;
const CHECK_SVG = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`;
const X_SVG = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;

class AfPill extends HTMLElement {
  constructor() {
    super();
    const root = this.attachShadow({ mode: 'open' });
    root.adoptedStyleSheets = [sheet];
    root.innerHTML = `<button type="button"><span class="icon">${SAVE_SVG}</span><span class="label">Save to AFFiNE</span></button>`;
    root.querySelector('button').addEventListener('click', e => this._onClick(e));
  }

  async _onClick(e) {
    e.preventDefault();
    e.stopPropagation();
    const btn = this.shadowRoot.querySelector('button');
    btn.disabled = true;
    btn.querySelector('.label').textContent = 'Saving…';
    btn.querySelector('.icon').innerHTML = SAVE_SVG;
    const url = this.dataset.url;
    const sourceApp = this.dataset.source ?? null;
    const sharedTitle = this.dataset.title ?? document.title;
    if (!url) { this._fail('No URL'); return; }
    try {
      const result = await dispatchCapture({ url, source_app: sourceApp, shared_title: sharedTitle });
      if (result?.ok) { this._ok(); }
      else { this._fail(result?.error?.kind ?? 'failed'); }
    } catch (err) {
      this._fail(err?.message ?? 'failed');
    }
  }

  _ok() {
    const btn = this.shadowRoot.querySelector('button');
    btn.classList.remove('err');
    btn.classList.add('ok');
    btn.querySelector('.icon').innerHTML = CHECK_SVG;
    btn.querySelector('.label').textContent = 'Saved';
    setTimeout(() => this._reset(), 1500);
  }
  _fail(kind) {
    const btn = this.shadowRoot.querySelector('button');
    btn.classList.remove('ok');
    btn.classList.add('err');
    btn.querySelector('.icon').innerHTML = X_SVG;
    btn.querySelector('.label').textContent = labelForFail(kind);
    setTimeout(() => this._reset(), 2000);
  }
  _reset() {
    const btn = this.shadowRoot.querySelector('button');
    btn.disabled = false;
    btn.classList.remove('ok', 'err');
    btn.querySelector('.icon').innerHTML = SAVE_SVG;
    btn.querySelector('.label').textContent = 'Save to AFFiNE';
  }
}

function labelForFail(kind) {
  switch (kind) {
    case 'invalid_token': return 'Token rejected';
    case 'config': return 'Not configured';
    case 'rate_limited': return 'Rate limited';
    case 'network': return 'Offline';
    default: return 'Failed';
  }
}

if (!customElements.get('af-pill')) {
  customElements.define('af-pill', AfPill);
}
```

- [ ] **Step 6: Implement `content/_shared/dispatch.js`**

```js
/**
 * Single content-script-side helper to send a capture request to the
 * background service worker. Used by the shared <af-pill> component.
 */
export async function dispatchCapture(payload) {
  return await chrome.runtime.sendMessage({ type: 'capture', payload });
}
```

- [ ] **Step 7: Update `manifest.json` — add `content_scripts`**

Add a new `content_scripts` field to the manifest (place it after `optional_host_permissions`):

```json
"content_scripts": [
  { "matches": ["*://*.youtube.com/*"], "js": ["content/youtube.js"], "run_at": "document_idle" },
  { "matches": ["*://*.instagram.com/*"], "js": ["content/instagram.js"], "run_at": "document_idle" },
  { "matches": ["*://x.com/*", "*://*.x.com/*", "*://twitter.com/*", "*://*.twitter.com/*"], "js": ["content/twitter.js"], "run_at": "document_idle" },
  { "matches": ["*://*.tiktok.com/*"], "js": ["content/tiktok.js"], "run_at": "document_idle" },
  { "matches": ["*://*.reddit.com/*"], "js": ["content/reddit.js"], "run_at": "document_idle" }
],
```

The 5 site files are created in Tasks 2 + 3. Loading the extension after Task 1 with these declarations but missing files → Chrome will log "could not load file" warnings but the rest of the extension keeps working. Tasks 2 + 3 fix this.

- [ ] **Step 8: Tests pass**

Expected: 86 / 86 (61 prior + 25 canonicalizer).

- [ ] **Step 9: Commit**

```sh
git add browser-extension/content/ browser-extension/manifest.json
git commit -m "feat(extension): <af-pill> + canonicalizers + manifest content_scripts (phase 8.1)"
```

---

## Task 2: YouTube + Instagram content scripts

**Files:**
- Create: `browser-extension/content/youtube.js`
- Create: `browser-extension/content/instagram.js`

- [ ] **Step 1: `content/youtube.js`**

```js
/**
 * YouTube content script. Injects an <af-pill> into the action row
 * (Save / Share buttons) of the watch page. Uses MutationObserver to
 * survive YouTube's SPA navigation.
 */
import { youtubeUrl } from './_shared/canonicalizers.js';
import './_shared/pill.js';

const ANCHOR_SELECTOR = 'ytd-watch-metadata #actions';
const PILL_FLAG = 'data-af-pill';

let observer = null;
let attemptedAt = 0;

attach();

function attach() {
  if (placePill()) return;
  if (observer) observer.disconnect();
  attemptedAt = Date.now();
  observer = new MutationObserver(() => {
    if (placePill()) return;
    if (Date.now() - attemptedAt > 10000) {
      console.warn('[AFFiNE Capture] anchor not found on youtube');
      observer.disconnect();
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });
}

function placePill() {
  const anchor = document.querySelector(ANCHOR_SELECTOR);
  if (!anchor) return false;
  if (anchor.querySelector(`af-pill[${PILL_FLAG}]`)) return true;
  const url = youtubeUrl(window.location.href);
  if (!url) return false;
  const pill = document.createElement('af-pill');
  pill.setAttribute(PILL_FLAG, '1');
  pill.dataset.url = url;
  pill.dataset.source = 'youtube';
  pill.dataset.title = document.title.replace(/ - YouTube$/, '');
  pill.style.marginLeft = '8px';
  anchor.appendChild(pill);
  return true;
}

// SPA navigation: listen to history.pushState (yt fires no consistent event).
const origPush = history.pushState;
history.pushState = function (...args) {
  origPush.apply(this, args);
  setTimeout(attach, 200);
};
window.addEventListener('popstate', () => setTimeout(attach, 200));
```

- [ ] **Step 2: `content/instagram.js`**

```js
/**
 * Instagram content script. Injects an <af-pill> per post/reel article
 * on hover. Targets `article[role=presentation]` containers.
 */
import { instagramUrl } from './_shared/canonicalizers.js';
import './_shared/pill.js';

const POST_SELECTOR = 'article[role=presentation]';
const PILL_FLAG = 'data-af-pill';
const HOVER_FLAG = 'data-af-hover';

const observer = new MutationObserver(scan);
observer.observe(document.body, { childList: true, subtree: true });
scan();

function scan() {
  const posts = document.querySelectorAll(POST_SELECTOR);
  for (const post of posts) {
    if (post.hasAttribute(HOVER_FLAG)) continue;
    post.setAttribute(HOVER_FLAG, '1');
    post.addEventListener('mouseenter', () => placePillIn(post));
  }
}

function placePillIn(post) {
  if (post.querySelector(`af-pill[${PILL_FLAG}]`)) return;
  // Look for the post permalink in the post DOM — the timestamp <a> typically
  // links to /p/<id>/ or /reel/<id>/.
  const link = post.querySelector('a[href*="/p/"], a[href*="/reel/"]');
  if (!link) return;
  const url = instagramUrl(link.href);
  if (!url) return;
  const pill = document.createElement('af-pill');
  pill.setAttribute(PILL_FLAG, '1');
  pill.dataset.url = url;
  pill.dataset.source = 'instagram';
  pill.style.position = 'absolute';
  pill.style.top = '8px';
  pill.style.right = '8px';
  pill.style.zIndex = '999';
  post.style.position = post.style.position || 'relative';
  post.appendChild(pill);
}
```

- [ ] **Step 3: Tests still green (86 / 86 — no new tests)**

```sh
npm test
```

- [ ] **Step 4: Commit**

```sh
git add browser-extension/content/youtube.js browser-extension/content/instagram.js
git commit -m "feat(extension): YouTube + Instagram content-script pills (phase 8.2)"
```

---

## Task 3: Twitter/X + TikTok + Reddit content scripts

**Files:**
- Create: `browser-extension/content/twitter.js`
- Create: `browser-extension/content/tiktok.js`
- Create: `browser-extension/content/reddit.js`

- [ ] **Step 1: `content/twitter.js`**

```js
/**
 * Twitter/X content script. Per-tweet pill on hover.
 * Anchor: `article[data-testid=tweet]`. Captures /<user>/status/<id>.
 */
import { twitterUrl } from './_shared/canonicalizers.js';
import './_shared/pill.js';

const TWEET_SELECTOR = 'article[data-testid=tweet]';
const PILL_FLAG = 'data-af-pill';
const HOVER_FLAG = 'data-af-hover';

const observer = new MutationObserver(scan);
observer.observe(document.body, { childList: true, subtree: true });
scan();

function scan() {
  for (const tweet of document.querySelectorAll(TWEET_SELECTOR)) {
    if (tweet.hasAttribute(HOVER_FLAG)) continue;
    tweet.setAttribute(HOVER_FLAG, '1');
    tweet.addEventListener('mouseenter', () => placePillIn(tweet));
  }
}

function placePillIn(tweet) {
  if (tweet.querySelector(`af-pill[${PILL_FLAG}]`)) return;
  const link = tweet.querySelector('a[href*="/status/"]');
  if (!link) return;
  const url = twitterUrl(link.href);
  if (!url) return;
  const pill = document.createElement('af-pill');
  pill.setAttribute(PILL_FLAG, '1');
  pill.dataset.url = url;
  pill.dataset.source = 'twitter';
  pill.style.position = 'absolute';
  pill.style.top = '8px';
  pill.style.right = '8px';
  pill.style.zIndex = '999';
  tweet.style.position = tweet.style.position || 'relative';
  tweet.appendChild(pill);
}
```

- [ ] **Step 2: `content/tiktok.js`**

```js
/**
 * TikTok content script. Per-FYP-card pill on hover.
 * Anchor: `div[data-e2e=recommend-list-item-container]` (FYP) or any element
 * with a /@user/video/<id> link nearby.
 */
import { tiktokUrl } from './_shared/canonicalizers.js';
import './_shared/pill.js';

const CARD_SELECTORS = [
  'div[data-e2e=recommend-list-item-container]',
  'div[data-e2e=user-post-item]',
];
const PILL_FLAG = 'data-af-pill';
const HOVER_FLAG = 'data-af-hover';

const observer = new MutationObserver(scan);
observer.observe(document.body, { childList: true, subtree: true });
scan();

function scan() {
  for (const sel of CARD_SELECTORS) {
    for (const card of document.querySelectorAll(sel)) {
      if (card.hasAttribute(HOVER_FLAG)) continue;
      card.setAttribute(HOVER_FLAG, '1');
      card.addEventListener('mouseenter', () => placePillIn(card));
    }
  }
}

function placePillIn(card) {
  if (card.querySelector(`af-pill[${PILL_FLAG}]`)) return;
  const link = card.querySelector('a[href*="/video/"]');
  if (!link) return;
  const url = tiktokUrl(link.href);
  if (!url) return;
  const pill = document.createElement('af-pill');
  pill.setAttribute(PILL_FLAG, '1');
  pill.dataset.url = url;
  pill.dataset.source = 'tiktok';
  pill.style.position = 'absolute';
  pill.style.top = '8px';
  pill.style.right = '8px';
  pill.style.zIndex = '999';
  card.style.position = card.style.position || 'relative';
  card.appendChild(pill);
}
```

- [ ] **Step 3: `content/reddit.js`**

```js
/**
 * Reddit content script. Per-post pill on hover.
 * Anchor: `shreddit-post` (new Reddit) or `[data-testid=post-container]`
 * (legacy fallback). Captures the comments permalink.
 */
import { redditUrl } from './_shared/canonicalizers.js';
import './_shared/pill.js';

const POST_SELECTORS = [
  'shreddit-post',
  '[data-testid=post-container]',
];
const PILL_FLAG = 'data-af-pill';
const HOVER_FLAG = 'data-af-hover';

const observer = new MutationObserver(scan);
observer.observe(document.body, { childList: true, subtree: true });
scan();

function scan() {
  for (const sel of POST_SELECTORS) {
    for (const post of document.querySelectorAll(sel)) {
      if (post.hasAttribute(HOVER_FLAG)) continue;
      post.setAttribute(HOVER_FLAG, '1');
      post.addEventListener('mouseenter', () => placePillIn(post));
    }
  }
}

function placePillIn(post) {
  if (post.querySelector(`af-pill[${PILL_FLAG}]`)) return;
  // shreddit-post has a permalink attr; legacy uses an <a> inside.
  const permalink = post.getAttribute?.('permalink')
    ?? post.querySelector('a[href*="/comments/"]')?.getAttribute('href');
  if (!permalink) return;
  // Resolve relative to absolute.
  const absolute = permalink.startsWith('http')
    ? permalink
    : `https://www.reddit.com${permalink}`;
  const url = redditUrl(absolute);
  if (!url) return;
  const pill = document.createElement('af-pill');
  pill.setAttribute(PILL_FLAG, '1');
  pill.dataset.url = url;
  pill.dataset.source = 'reddit';
  pill.style.position = 'absolute';
  pill.style.top = '8px';
  pill.style.right = '8px';
  pill.style.zIndex = '999';
  post.style.position = post.style.position || 'relative';
  post.appendChild(pill);
}
```

- [ ] **Step 4: Tests still green (86 / 86)**

- [ ] **Step 5: Commit**

```sh
git add browser-extension/content/twitter.js browser-extension/content/tiktok.js browser-extension/content/reddit.js
git commit -m "feat(extension): Twitter/X + TikTok + Reddit content-script pills (phase 8.3)"
```

---

## Task 4: Manual smoke (USER)

After Tasks 1–3 land, reload the extension and visit each site:

- [ ] **YouTube**: open any watch page → blue pill appears next to native Save/Share row (may take 1–2s to render after initial load).
  - Click pill → "Saving…" → "Saved" (green) for 1.5s → resets.
  - Navigate to a different video (SPA route) → pill re-renders for the new video URL.
  - Verify in History tab that the captured URL is `?v=<id>` (no playlist params).
- [ ] **Instagram**: open `instagram.com/` feed → hover any post → pill appears top-right of the article.
  - Click → captures `/p/<id>/` or `/reel/<id>/`.
- [ ] **X (twitter)**: open `x.com/<user>` → hover any tweet → pill appears top-right.
  - Click → captures `/status/<id>`.
- [ ] **TikTok**: open `tiktok.com/foryou` → hover a FYP card → pill appears.
  - Click → captures `@<user>/video/<id>`.
- [ ] **Reddit**: open `reddit.com/r/programming` → hover any post → pill appears.
  - Click → captures the comments permalink.
- [ ] **Anchor-not-found behavior**: open YouTube but immediately navigate away — `[AFFiNE Capture] anchor not found on youtube` warning logged after 10s, no crash.
- [ ] **Visual isolation**: open `chrome://extensions` → Inspect a content-script-injected page in DevTools → set `body { all: initial !important }` in Elements → pill remains correctly styled (Shadow DOM isolation).
- [ ] **No double-pills**: scroll IG/X feed extensively → only one pill per post.

Brittleness note: site DOMs change. Anchor selectors will rot every few months. Each site script logs a warning when its observer fails — that's the early signal to update selectors.
