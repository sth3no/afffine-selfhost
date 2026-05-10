/**
 * <af-status-timeline status="queued|extracting|classifying|filing|done|failed">
 *
 * 5-step horizontal timeline. Steps before/at the current status are
 * highlighted in af-blue; steps after are subdued. Failed status puts
 * a red marker at whatever step the pipeline reached.
 */

const STEPS = ['queued', 'extracting', 'classifying', 'filing', 'done'];

const componentSheet = new CSSStyleSheet();
componentSheet.replaceSync(`
  :host { display: block; }
  .timeline {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: var(--af-space-1);
    margin: var(--af-space-3) 0;
  }
  .step {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--af-space-1);
    font: var(--af-small);
    color: var(--af-text-body);
  }
  .step .dot {
    width: 12px; height: 12px;
    border-radius: 50%;
    background: var(--af-border);
  }
  .step .bar {
    width: 100%;
    height: 2px;
    background: var(--af-border);
    margin-top: -7px;
  }
  .step.passed .dot,
  .step.passed .bar { background: var(--af-blue); }
  .step.current .dot {
    background: var(--af-blue);
    box-shadow: 0 0 0 4px rgba(43, 133, 255, .15);
  }
  .step.current { color: var(--af-blue); font-weight: 600; }
  .step.failed .dot { background: var(--af-error); }
  .step.failed { color: var(--af-error); font-weight: 600; }
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

class AfStatusTimeline extends HTMLElement {
  static observedAttributes = ['status', 'failed-at'];

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
    const status = this.getAttribute('status') ?? 'queued';
    const failedAt = this.getAttribute('failed-at');
    const currentIdx = status === 'failed'
      ? STEPS.indexOf(failedAt ?? 'extracting')
      : STEPS.indexOf(status);

    const html = STEPS.map((step, i) => {
      const classes = ['step'];
      if (status === 'failed' && i === currentIdx) classes.push('failed');
      else if (i < currentIdx) classes.push('passed');
      else if (i === currentIdx) classes.push('current');
      return `<div class="${classes.join(' ')}" data-step="${step}">
        <div class="dot"></div>
        <div class="label">${step}</div>
      </div>`;
    }).join('');

    this.shadowRoot.innerHTML = `<div class="timeline">${html}</div>`;
  }
}

customElements.define('af-status-timeline', AfStatusTimeline);
