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
