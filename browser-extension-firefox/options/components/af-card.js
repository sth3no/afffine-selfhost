/**
 * <af-card>
 *
 * Plain styled wrapper: rounded corners, subtle shadow, 24 px padding.
 * Useful for grouping related controls (Settings tab, History rows).
 */

const componentSheet = new CSSStyleSheet();
componentSheet.replaceSync(`
  :host {
    display: block;
    background: var(--af-surface);
    border: 1px solid var(--af-border);
    border-radius: var(--af-radius-card);
    box-shadow: var(--af-shadow-card);
    padding: var(--af-space-4);
  }
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

class AfCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `<slot></slot>`;
    this.shadowRoot.adoptedStyleSheets = [componentSheet];
  }

  async connectedCallback() {
    const sheet = await getTokensSheet();
    this.shadowRoot.adoptedStyleSheets = [sheet, componentSheet];
  }
}

customElements.define('af-card', AfCard);
