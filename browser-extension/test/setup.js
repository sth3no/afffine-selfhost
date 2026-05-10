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

chrome.runtime.getURL = vi.fn((path) => `chrome-extension://test/${path}`);

// jsdom 25.0.1 exposes the CSSStyleSheet constructor but does not implement
// the Constructable Stylesheets API (replaceSync / replace / adoptedStyleSheets).
// Polyfill the minimum needed so <af-button> and sibling components can run
// under jsdom without any change to the production component code.
if (typeof CSSStyleSheet !== 'undefined' && !CSSStyleSheet.prototype.replaceSync) {
  CSSStyleSheet.prototype.replaceSync = function replaceSync(_css) {
    // no-op: jsdom won't apply the CSS visually; the tests only check
    // DOM structure / class names / disabled state, not computed styles.
  };
  CSSStyleSheet.prototype.replace = async function replace(_css) {
    return this;
  };
}
// jsdom ShadowRoot also lacks adoptedStyleSheets. Patch it via the prototype
// after the first shadow root is created — or just guard at assignment time
// by making af-button tolerate a missing setter.  The simplest shim:
if (typeof ShadowRoot !== 'undefined') {
  Object.defineProperty(ShadowRoot.prototype, 'adoptedStyleSheets', {
    get() { return this._adoptedStyleSheets ?? []; },
    set(sheets) { this._adoptedStyleSheets = sheets; },
    configurable: true,
  });
}
if (typeof Document !== 'undefined' && !Object.getOwnPropertyDescriptor(Document.prototype, 'adoptedStyleSheets')) {
  Object.defineProperty(Document.prototype, 'adoptedStyleSheets', {
    get() { return this._adoptedStyleSheets ?? []; },
    set(sheets) { this._adoptedStyleSheets = sheets; },
    configurable: true,
  });
}

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
