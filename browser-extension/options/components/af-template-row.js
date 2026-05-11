/**
 * <af-template-row> — one row in the Templates tab.
 *
 * Properties (set via .data getter/setter — fields don't fit cleanly into HTML attrs):
 *   { id, platform_id, topic, name, status, usage_count, ... ContentTemplateView }
 *
 * Events (composed: true so they bubble out of Shadow DOM):
 *   - 'open'    — body click; detail = full row data
 *   - 'archive' — archive button click; detail = full row data
 */
import { trashIcon } from '../../lib/icons.js';

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
    grid-template-columns: 1fr auto auto auto;
    align-items: center;
    gap: var(--af-space-3);
    padding: var(--af-space-3);
  }

  .body {
    cursor: pointer;
    display: flex;
    flex-direction: column;
    gap: 2px;
    overflow: hidden;
  }
  .name {
    font: var(--af-body);
    font-weight: 600;
    color: var(--af-navy);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .scope {
    font: var(--af-small);
    color: var(--af-text-body);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .pill {
    display: inline-block;
    padding: 2px var(--af-space-2);
    border-radius: var(--af-radius-pill);
    font: var(--af-small);
    font-weight: 600;
    text-transform: capitalize;
  }
  .pill.auto { background: var(--af-bg-soft); color: var(--af-blue); }
  .pill.edited { background: #E6F6EA; color: var(--af-success); }
  .pill.archived { background: var(--af-gray-50); color: var(--af-text-body); }

  .usage {
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
  button.icon-btn.archive:hover { background: var(--af-bg-soft); color: var(--af-error); }
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

class AfTemplateRow extends HTMLElement {
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
    const isSeed = d.platform_id === '*' && d.topic === '*';
    const scopeText = isSeed
      ? `(*, *) — global default`
      : `${d.platform_id} · ${d.topic}`;
    const showArchive = d.status !== 'archived';
    this.shadowRoot.innerHTML = `
      <div class="row">
        <div class="body">
          <div class="name">${escapeHtml(d.name)}</div>
          <div class="scope">${escapeHtml(scopeText)}</div>
        </div>
        <span class="pill ${escapeHtml(d.status)}">${escapeHtml(d.status)}</span>
        <span class="usage">${Number(d.usage_count ?? 0)} uses</span>
        <span class="actions">
          ${showArchive ? `<button class="icon-btn archive" title="Archive">${trashIcon}</button>` : ''}
        </span>
      </div>
    `;
    this.shadowRoot.querySelector('.body').addEventListener('click', () => {
      this.dispatchEvent(new CustomEvent('open', {
        detail: this._data, bubbles: true, composed: true,
      }));
    });
    const archive = this.shadowRoot.querySelector('button.archive');
    if (archive) archive.addEventListener('click', e => {
      e.stopPropagation();
      this.dispatchEvent(new CustomEvent('archive', {
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

customElements.define('af-template-row', AfTemplateRow);
