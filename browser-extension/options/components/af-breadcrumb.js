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
