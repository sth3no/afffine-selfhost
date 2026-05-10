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
import { platformIcon, arrowClockwiseIcon, trashIcon } from '../../lib/icons.js';
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
    this._render();
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
