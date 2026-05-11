/**
 * <af-prompt-textarea rows="30" placeholder="…" value="…">
 *
 * Monospace textarea wrapper for editing template system_prompts. Mirrors
 * <af-input>'s API: .value getter/setter; bubbles native `input` and
 * `change` events out of the Shadow DOM.
 */

const componentSheet = new CSSStyleSheet();
componentSheet.replaceSync(`
  :host { display: block; }
  .wrap {
    border: 1px solid var(--af-border);
    border-radius: var(--af-radius-button);
    background: var(--af-surface);
    overflow: hidden;
  }
  .wrap:focus-within { border-color: var(--af-blue); }
  textarea {
    width: 100%;
    box-sizing: border-box;
    border: none;
    outline: none;
    padding: var(--af-space-3);
    font-family: var(--af-font-mono);
    font-size: 13px;
    line-height: 1.5;
    color: var(--af-navy);
    background: transparent;
    resize: vertical;
  }
  textarea::placeholder { color: var(--af-text-body); }
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

class AfPromptTextarea extends HTMLElement {
  static observedAttributes = ['rows', 'placeholder', 'value'];

  constructor() {
    super();
    const root = this.attachShadow({ mode: 'open' });
    root.innerHTML = `<div class="wrap"><textarea rows="30"></textarea></div>`;
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

  get value() { return this.shadowRoot.querySelector('textarea')?.value ?? ''; }
  set value(v) {
    const ta = this.shadowRoot.querySelector('textarea');
    if (ta) ta.value = v ?? '';
  }

  _sync() {
    const ta = this.shadowRoot.querySelector('textarea');
    if (!ta) return;
    ta.rows = Number(this.getAttribute('rows')) || 30;
    if (this.hasAttribute('placeholder')) ta.placeholder = this.getAttribute('placeholder');
    if (this.hasAttribute('value')) ta.value = this.getAttribute('value');
  }

  _wire() {
    const ta = this.shadowRoot.querySelector('textarea');
    ta.addEventListener('input', () => {
      this.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
    });
    ta.addEventListener('change', () => {
      this.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
    });
  }
}

customElements.define('af-prompt-textarea', AfPromptTextarea);
