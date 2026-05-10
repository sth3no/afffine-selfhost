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
        } catch { /* ignore */ }
      });
      wrap.appendChild(btn);
    } else if (!want && exists) {
      exists.remove();
    }
  }

  _wire() {
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
