/**
 * <af-template-editor> — detail/edit form for one content template.
 *
 * Properties:
 *   .data = ContentTemplateView | null
 *
 * Events (composed: true, bubbles out of Shadow DOM):
 *   - 'save'     — detail = { id, patch: { name, system_prompt } }
 *   - 'archive'  — detail = { id, name }
 *   - 'resynth'  — detail = { id, platform_id, topic }
 *   - 'apply'    — detail = { id, platform_id, topic }
 *   - 'back'     — no detail (parent handles navigation)
 *
 * The parent (options.js) performs the API calls; this component is
 * purely presentational + form-state management.
 */
import './af-button.js';
import './af-input.js';
import './af-prompt-textarea.js';

const componentSheet = new CSSStyleSheet();
componentSheet.replaceSync(`
  :host {
    display: block;
    background: var(--af-surface);
    border: 1px solid var(--af-border);
    border-radius: var(--af-radius-card);
    padding: var(--af-space-4);
    box-shadow: var(--af-shadow-card);
  }
  .back-btn {
    background: transparent; border: none;
    color: var(--af-blue); cursor: pointer;
    font: var(--af-small); padding: 0;
    margin-bottom: var(--af-space-3);
  }
  .back-btn:hover { text-decoration: underline; }
  .meta-header {
    display: flex; flex-wrap: wrap;
    gap: var(--af-space-2);
    font: var(--af-small);
    color: var(--af-text-body);
    margin-bottom: var(--af-space-3);
  }
  .meta-header .scope {
    color: var(--af-navy);
    font-weight: 600;
  }
  .field-label {
    display: block;
    margin-top: var(--af-space-3);
    margin-bottom: var(--af-space-1);
    font: var(--af-small);
    font-weight: 600;
    color: var(--af-navy);
  }
  .generator-meta {
    background: var(--af-bg-soft);
    border-radius: var(--af-radius-button);
    padding: var(--af-space-3);
    margin-top: var(--af-space-3);
    font: var(--af-small);
    color: var(--af-text-body);
  }
  .generator-meta .gm-row {
    margin-bottom: var(--af-space-1);
  }
  .generator-meta .gm-label {
    color: var(--af-navy);
    font-weight: 600;
    margin-right: var(--af-space-1);
  }
  .help {
    background: var(--af-gray-50);
    border-radius: var(--af-radius-button);
    padding: var(--af-space-3);
    margin-top: var(--af-space-3);
    font: var(--af-small);
    color: var(--af-text-body);
  }
  .help summary {
    cursor: pointer;
    font-weight: 600;
    color: var(--af-navy);
  }
  .help ul { margin: var(--af-space-2) 0 0; padding-left: var(--af-space-4); }
  .actions {
    display: flex; flex-wrap: wrap;
    gap: var(--af-space-2);
    margin-top: var(--af-space-4);
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

class AfTemplateEditor extends HTMLElement {
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
    if (!this._data) {
      this.shadowRoot.innerHTML = `<p class="hint">No template selected.</p>`;
      return;
    }
    const d = this._data;
    const isSeed = d.platform_id === '*' && d.topic === '*';
    const scope = isSeed
      ? '(*, *) — global default'
      : `${d.platform_id} · ${d.topic}`;

    const generatorMetaHtml = (d.status === 'auto' && d.generator_meta)
      ? renderGeneratorMeta(d.generator_meta)
      : '';
    const archiveBtn = d.status === 'archived'
      ? ''
      : `<af-button class="archive-btn" variant="ghost">Archive</af-button>`;

    this.shadowRoot.innerHTML = `
      <button class="back-btn" type="button">← Back to templates</button>

      <div class="meta-header">
        <span class="scope">${escapeHtml(scope)}</span>
        <span>status: ${escapeHtml(d.status)}</span>
        <span>created by ${escapeHtml(d.created_by)}</span>
        <span>used in ${Number(d.usage_count ?? 0)} captures</span>
      </div>

      <label class="field-label">Name</label>
      <af-input class="name-input" type="text" value="${escapeAttr(d.name)}"></af-input>

      <label class="field-label">System prompt</label>
      <af-prompt-textarea rows="30" value="${escapeAttr(d.system_prompt)}"></af-prompt-textarea>

      ${generatorMetaHtml}

      <details class="help">
        <summary>AFFiNE markdown reference</summary>
        <ul>
          <li>Headings (<code># H1</code> – <code>###### H6</code>), bold, italic, links</li>
          <li>Lists, including <code>- [ ]</code> todos</li>
          <li>Fenced code blocks (any language + <code>mermaid</code> + <code>embed-html</code>)</li>
          <li>Callouts (<code>&gt; [!NOTE]</code> / <code>[!WARNING]</code> / <code>[!TIP]</code>)</li>
          <li>Keyframe refs: <code>kf:0</code>, <code>kf:1</code>, … inline</li>
          <li>Cross-doc refs: <code>[[Doc Title]]</code></li>
        </ul>
      </details>

      <div class="actions">
        <af-button class="save-btn" variant="primary">Save</af-button>
        ${archiveBtn}
        <af-button class="resynth-btn" variant="secondary">Re-synthesize from sample capture</af-button>
        <af-button class="apply-btn" variant="secondary">Apply to existing capture…</af-button>
      </div>
    `;

    this.shadowRoot.querySelector('.back-btn').addEventListener('click', () => {
      this.dispatchEvent(new CustomEvent('back', { bubbles: true, composed: true }));
    });
    this.shadowRoot.querySelector('af-button.save-btn').addEventListener('click', () => {
      const name = this.shadowRoot.querySelector('af-input.name-input').value;
      const system_prompt = this.shadowRoot.querySelector('af-prompt-textarea').value;
      this.dispatchEvent(new CustomEvent('save', {
        detail: { id: d.id, patch: { name, system_prompt } },
        bubbles: true, composed: true,
      }));
    });
    const archiveEl = this.shadowRoot.querySelector('af-button.archive-btn');
    if (archiveEl) archiveEl.addEventListener('click', () => {
      this.dispatchEvent(new CustomEvent('archive', {
        detail: { id: d.id, name: d.name }, bubbles: true, composed: true,
      }));
    });
    this.shadowRoot.querySelector('af-button.resynth-btn').addEventListener('click', () => {
      this.dispatchEvent(new CustomEvent('resynth', {
        detail: { id: d.id, platform_id: d.platform_id, topic: d.topic },
        bubbles: true, composed: true,
      }));
    });
    this.shadowRoot.querySelector('af-button.apply-btn').addEventListener('click', () => {
      this.dispatchEvent(new CustomEvent('apply', {
        detail: { id: d.id, platform_id: d.platform_id, topic: d.topic },
        bubbles: true, composed: true,
      }));
    });
  }
}

function renderGeneratorMeta(meta) {
  const blocks = Array.isArray(meta.available_blocks_used)
    ? meta.available_blocks_used.join(', ')
    : '';
  return `
    <div class="generator-meta">
      <div class="gm-row"><span class="gm-label">Biggest value:</span> ${escapeHtml(meta.biggest_value ?? '')}</div>
      <div class="gm-row"><span class="gm-label">User intent:</span> ${escapeHtml(meta.user_intent ?? '')}</div>
      <div class="gm-row"><span class="gm-label">Best ROI format:</span> ${escapeHtml(meta.best_roi_format ?? '')}</div>
      <div class="gm-row"><span class="gm-label">Blocks used:</span> ${escapeHtml(blocks)}</div>
      <div class="gm-row"><span class="gm-label">Synthesized by:</span> ${escapeHtml(meta.synthesizer_model ?? '')} at ${escapeHtml(meta.synthesized_at ?? '')}</div>
    </div>
  `;
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
function escapeAttr(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/"/g, '&quot;');
}

customElements.define('af-template-editor', AfTemplateEditor);
