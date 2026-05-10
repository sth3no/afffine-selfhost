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
