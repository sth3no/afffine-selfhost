/**
 * <af-pill data-url="..." data-source="...">
 *
 * Floating in-page pill. Shadow-DOM rooted so host CSS can't break it.
 * On click, dispatches a capture message and animates a checkmark on success.
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
