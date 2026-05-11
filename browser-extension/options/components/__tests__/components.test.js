/** @vitest-environment jsdom */
import { describe, it, expect, beforeAll } from 'vitest';
import '../af-button.js';

describe('<af-button>', () => {
  it('registers the custom element', () => {
    expect(customElements.get('af-button')).toBeTypeOf('function');
  });

  it('renders a button inside Shadow DOM', () => {
    const el = document.createElement('af-button');
    el.textContent = 'Save';
    document.body.appendChild(el);
    const btn = el.shadowRoot?.querySelector('button');
    expect(btn).toBeTruthy();
  });

  it('reflects variant=primary by default', () => {
    const el = document.createElement('af-button');
    document.body.appendChild(el);
    const btn = el.shadowRoot.querySelector('button');
    expect(btn.classList.contains('primary')).toBe(true);
  });

  it('reflects variant=secondary attribute', () => {
    const el = document.createElement('af-button');
    el.setAttribute('variant', 'secondary');
    document.body.appendChild(el);
    const btn = el.shadowRoot.querySelector('button');
    expect(btn.classList.contains('secondary')).toBe(true);
    expect(btn.classList.contains('primary')).toBe(false);
  });

  it('disabled attribute disables the inner button', () => {
    const el = document.createElement('af-button');
    el.setAttribute('disabled', '');
    document.body.appendChild(el);
    expect(el.shadowRoot.querySelector('button').disabled).toBe(true);
  });

  it('forwards click events from inner button', async () => {
    const el = document.createElement('af-button');
    document.body.appendChild(el);
    let clicks = 0;
    el.addEventListener('click', () => clicks++);
    el.shadowRoot.querySelector('button').click();
    expect(clicks).toBe(1);
  });
});

import '../af-input.js';

describe('<af-input>', () => {
  it('registers the custom element', () => {
    expect(customElements.get('af-input')).toBeTypeOf('function');
  });

  it('renders an input inside Shadow DOM', () => {
    const el = document.createElement('af-input');
    document.body.appendChild(el);
    expect(el.shadowRoot.querySelector('input')).toBeTruthy();
  });

  it('reflects type=password attribute', () => {
    const el = document.createElement('af-input');
    el.setAttribute('type', 'password');
    document.body.appendChild(el);
    expect(el.shadowRoot.querySelector('input').type).toBe('password');
  });

  it('value getter/setter round-trip', () => {
    const el = document.createElement('af-input');
    document.body.appendChild(el);
    el.value = 'hello';
    expect(el.value).toBe('hello');
    expect(el.shadowRoot.querySelector('input').value).toBe('hello');
  });

  it('paste-button attribute renders a paste button', () => {
    const el = document.createElement('af-input');
    el.setAttribute('paste-button', '');
    document.body.appendChild(el);
    expect(el.shadowRoot.querySelector('button.paste')).toBeTruthy();
  });

  it('emits change event when input changes', () => {
    const el = document.createElement('af-input');
    document.body.appendChild(el);
    let received = null;
    el.addEventListener('change', e => { received = e.target.value; });
    const inner = el.shadowRoot.querySelector('input');
    inner.value = 'new';
    inner.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
    expect(received).toBe('new');
  });
});

import '../af-status-badge.js';

describe('<af-status-badge>', () => {
  it('registers', () => {
    expect(customElements.get('af-status-badge')).toBeTypeOf('function');
  });

  it('renders done with green check', () => {
    const el = document.createElement('af-status-badge');
    el.setAttribute('status', 'done');
    document.body.appendChild(el);
    const root = el.shadowRoot;
    expect(root.querySelector('.done')).toBeTruthy();
    expect(root.innerHTML).toContain('<polyline');  // checkIcon
  });

  it('renders failed with red x', () => {
    const el = document.createElement('af-status-badge');
    el.setAttribute('status', 'failed');
    document.body.appendChild(el);
    expect(el.shadowRoot.querySelector('.failed')).toBeTruthy();
  });

  it('renders queued/extracting/classifying/filing as in-progress', () => {
    for (const s of ['queued', 'extracting', 'classifying', 'filing']) {
      const el = document.createElement('af-status-badge');
      el.setAttribute('status', s);
      document.body.appendChild(el);
      expect(el.shadowRoot.querySelector('.in-progress'), s).toBeTruthy();
    }
  });

  it('renders fallback for unknown status', () => {
    const el = document.createElement('af-status-badge');
    el.setAttribute('status', 'mystery');
    document.body.appendChild(el);
    expect(el.shadowRoot.querySelector('.unknown')).toBeTruthy();
  });
});

import '../af-card.js';

describe('<af-card>', () => {
  it('registers', () => {
    expect(customElements.get('af-card')).toBeTypeOf('function');
  });

  it('slots arbitrary content', () => {
    const el = document.createElement('af-card');
    el.innerHTML = '<p>hello</p>';
    document.body.appendChild(el);
    expect(el.querySelector('p')?.textContent).toBe('hello');
  });

  it('has a slot inside the shadow root', () => {
    const el = document.createElement('af-card');
    document.body.appendChild(el);
    expect(el.shadowRoot.querySelector('slot')).toBeTruthy();
  });
});

import '../af-history-row.js';
import '../af-status-timeline.js';
import '../af-breadcrumb.js';

describe('<af-history-row>', () => {
  function makeRow(props = {}) {
    const el = document.createElement('af-history-row');
    el.data = {
      capture_id: '01ABC',
      platform: 'youtube',
      status: 'done',
      shared_title: 'My video',
      topic_path: 'Sources/Videos/YouTube',
      created_at: new Date().toISOString(),
      web_url: 'https://example.com/doc',
      ...props,
    };
    document.body.appendChild(el);
    return el;
  }

  it('registers', () => {
    expect(customElements.get('af-history-row')).toBeTypeOf('function');
  });

  it('renders title from data', () => {
    const el = makeRow({ shared_title: 'Hello world' });
    expect(el.shadowRoot.querySelector('.title')?.textContent).toBe('Hello world');
  });

  it('renders topic_path subtle below title', () => {
    const el = makeRow({ topic_path: 'Sources/X/Y' });
    expect(el.shadowRoot.querySelector('.path')?.textContent).toBe('Sources/X/Y');
  });

  it('emits "open" custom event on body click', () => {
    const el = makeRow();
    let received = null;
    el.addEventListener('open', e => { received = e.detail; });
    el.shadowRoot.querySelector('.body').click();
    expect(received?.capture_id).toBe('01ABC');
  });

  it('emits "retry" on retry button click', () => {
    const el = makeRow({ status: 'failed' });
    let received = null;
    el.addEventListener('retry', e => { received = e.detail; });
    el.shadowRoot.querySelector('button.retry')?.click();
    expect(received?.capture_id).toBe('01ABC');
  });

  it('emits "delete" on delete button click', () => {
    const el = makeRow();
    let received = null;
    el.addEventListener('delete', e => { received = e.detail; });
    el.shadowRoot.querySelector('button.delete')?.click();
    expect(received?.capture_id).toBe('01ABC');
  });
});

describe('<af-status-timeline>', () => {
  it('registers', () => {
    expect(customElements.get('af-status-timeline')).toBeTypeOf('function');
  });

  it('marks current step as active', () => {
    const el = document.createElement('af-status-timeline');
    el.setAttribute('status', 'classifying');
    document.body.appendChild(el);
    const steps = el.shadowRoot.querySelectorAll('.step');
    expect(steps.length).toBe(5);
    const active = el.shadowRoot.querySelector('.step.current');
    expect(active?.dataset.step).toBe('classifying');
  });

  it('marks failed status with error step', () => {
    const el = document.createElement('af-status-timeline');
    el.setAttribute('status', 'failed');
    document.body.appendChild(el);
    expect(el.shadowRoot.querySelector('.failed')).toBeTruthy();
  });
});

describe('<af-breadcrumb>', () => {
  it('registers', () => {
    expect(customElements.get('af-breadcrumb')).toBeTypeOf('function');
  });

  it('splits path into segments', () => {
    const el = document.createElement('af-breadcrumb');
    el.setAttribute('path', 'Sources/Socials/Instagram/Recipes');
    document.body.appendChild(el);
    const segments = el.shadowRoot.querySelectorAll('.segment');
    expect(segments.length).toBe(4);
    expect(segments[0].textContent).toBe('Sources');
    expect(segments[3].textContent).toBe('Recipes');
  });
});

import '../af-template-row.js';

describe('<af-template-row>', () => {
  function makeRow(props = {}) {
    const el = document.createElement('af-template-row');
    el.data = {
      id: '01TPL',
      platform_id: 'youtube',
      topic: 'Tutorials',
      name: 'YouTube Tutorial v1',
      system_prompt: '...',
      status: 'edited',
      generator_meta: null,
      created_by: 'user',
      created_at: '2026-05-11T14:32:00Z',
      updated_at: '2026-05-11T15:00:00Z',
      usage_count: 14,
      ...props,
    };
    document.body.appendChild(el);
    return el;
  }

  it('registers', () => {
    expect(customElements.get('af-template-row')).toBeTypeOf('function');
  });

  it('renders the template name', () => {
    const el = makeRow({ name: 'My Template' });
    expect(el.shadowRoot.querySelector('.name')?.textContent).toBe('My Template');
  });

  it('renders scope as platform · topic', () => {
    const el = makeRow({ platform_id: 'youtube', topic: 'Tutorials' });
    expect(el.shadowRoot.querySelector('.scope')?.textContent).toBe('youtube · Tutorials');
  });

  it('renders scope (*, *) with a "global default" label', () => {
    const el = makeRow({ platform_id: '*', topic: '*' });
    expect(el.shadowRoot.querySelector('.scope')?.textContent).toContain('global default');
  });

  it('renders the status pill class', () => {
    const el = makeRow({ status: 'auto' });
    expect(el.shadowRoot.querySelector('.pill.auto')).toBeTruthy();
  });

  it('renders usage count', () => {
    const el = makeRow({ usage_count: 14 });
    expect(el.shadowRoot.textContent).toContain('14');
  });

  it('emits "open" event on body click', () => {
    const el = makeRow();
    let received = null;
    el.addEventListener('open', e => { received = e.detail; });
    el.shadowRoot.querySelector('.body').click();
    expect(received?.id).toBe('01TPL');
  });

  it('emits "archive" event on archive button click', () => {
    const el = makeRow();
    let received = null;
    el.addEventListener('archive', e => { received = e.detail; });
    el.shadowRoot.querySelector('button.archive')?.click();
    expect(received?.id).toBe('01TPL');
  });

  it('hides the archive button for status=archived', () => {
    const el = makeRow({ status: 'archived' });
    expect(el.shadowRoot.querySelector('button.archive')).toBeFalsy();
  });
});

import '../af-prompt-textarea.js';

describe('<af-prompt-textarea>', () => {
  it('registers the custom element', () => {
    expect(customElements.get('af-prompt-textarea')).toBeTypeOf('function');
  });

  it('renders a textarea inside Shadow DOM', () => {
    const el = document.createElement('af-prompt-textarea');
    document.body.appendChild(el);
    expect(el.shadowRoot.querySelector('textarea')).toBeTruthy();
  });

  it('defaults to 30 rows', () => {
    const el = document.createElement('af-prompt-textarea');
    document.body.appendChild(el);
    expect(el.shadowRoot.querySelector('textarea').rows).toBe(30);
  });

  it('honours rows attribute override', () => {
    const el = document.createElement('af-prompt-textarea');
    el.setAttribute('rows', '12');
    document.body.appendChild(el);
    expect(el.shadowRoot.querySelector('textarea').rows).toBe(12);
  });

  it('value getter/setter round-trips', () => {
    const el = document.createElement('af-prompt-textarea');
    document.body.appendChild(el);
    el.value = 'hello world';
    expect(el.value).toBe('hello world');
    expect(el.shadowRoot.querySelector('textarea').value).toBe('hello world');
  });

  it('emits input and change events from the inner textarea', () => {
    const el = document.createElement('af-prompt-textarea');
    document.body.appendChild(el);
    let inputs = 0, changes = 0;
    el.addEventListener('input', () => inputs++);
    el.addEventListener('change', () => changes++);
    const ta = el.shadowRoot.querySelector('textarea');
    ta.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
    ta.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
    expect(inputs).toBe(1);
    expect(changes).toBe(1);
  });

  it('placeholder attribute populates the textarea', () => {
    const el = document.createElement('af-prompt-textarea');
    el.setAttribute('placeholder', 'system prompt…');
    document.body.appendChild(el);
    expect(el.shadowRoot.querySelector('textarea').placeholder).toBe('system prompt…');
  });
});

import '../af-template-editor.js';

describe('<af-template-editor>', () => {
  function makeEditor(props = {}) {
    const el = document.createElement('af-template-editor');
    el.data = {
      id: '01TPL',
      platform_id: 'youtube',
      topic: 'Tutorials',
      name: 'YouTube Tutorial v1',
      system_prompt: 'You are…',
      status: 'edited',
      generator_meta: null,
      created_by: 'user',
      created_at: '2026-05-11T14:32:00Z',
      updated_at: '2026-05-11T15:00:00Z',
      usage_count: 14,
      ...props,
    };
    document.body.appendChild(el);
    return el;
  }

  it('registers', () => {
    expect(customElements.get('af-template-editor')).toBeTypeOf('function');
  });

  it('renders the name in an editable input', () => {
    const el = makeEditor({ name: 'My Name' });
    const nameInput = el.shadowRoot.querySelector('af-input.name-input');
    expect(nameInput).toBeTruthy();
  });

  it('renders the system_prompt in the textarea', () => {
    const el = makeEditor({ system_prompt: 'You are X' });
    const ta = el.shadowRoot.querySelector('af-prompt-textarea');
    expect(ta).toBeTruthy();
  });

  it('renders the generator_meta block when status=auto', () => {
    const el = makeEditor({
      status: 'auto',
      generator_meta: {
        biggest_value: 'Step-by-step',
        user_intent: 'Bookmark',
        best_roi_format: 'Numbered list',
        available_blocks_used: ['paragraph', 'list'],
        synthesizer_model: 'claude-sonnet-4-6',
        synthesized_at: '2026-05-11T14:32:00Z',
      },
    });
    const meta = el.shadowRoot.querySelector('.generator-meta');
    expect(meta).toBeTruthy();
    expect(meta.textContent).toContain('Step-by-step');
  });

  it('omits generator_meta block when null', () => {
    const el = makeEditor({ status: 'edited', generator_meta: null });
    expect(el.shadowRoot.querySelector('.generator-meta')).toBeFalsy();
  });

  it('renders scope header with platform_id and topic', () => {
    const el = makeEditor({ platform_id: 'youtube', topic: 'Tutorials' });
    const header = el.shadowRoot.querySelector('.meta-header');
    expect(header.textContent).toContain('youtube');
    expect(header.textContent).toContain('Tutorials');
  });

  it('emits "save" with current name + system_prompt on save click', () => {
    const el = makeEditor();
    let received = null;
    el.addEventListener('save', e => { received = e.detail; });
    el.shadowRoot.querySelector('af-prompt-textarea').value = 'new prompt';
    el.shadowRoot.querySelector('af-input.name-input').value = 'new name';
    el.shadowRoot.querySelector('af-button.save-btn').dispatchEvent(new Event('click'));
    expect(received).toEqual({
      id: '01TPL',
      patch: { name: 'new name', system_prompt: 'new prompt' },
    });
  });

  it('emits "archive" with id on archive click', () => {
    const el = makeEditor();
    let received = null;
    el.addEventListener('archive', e => { received = e.detail; });
    el.shadowRoot.querySelector('af-button.archive-btn').dispatchEvent(new Event('click'));
    expect(received).toEqual({ id: '01TPL', name: 'YouTube Tutorial v1' });
  });

  it('emits "resynth" with platform_id + topic', () => {
    const el = makeEditor();
    let received = null;
    el.addEventListener('resynth', e => { received = e.detail; });
    el.shadowRoot.querySelector('af-button.resynth-btn').dispatchEvent(new Event('click'));
    expect(received).toEqual({ id: '01TPL', platform_id: 'youtube', topic: 'Tutorials' });
  });

  it('emits "apply" with current template data', () => {
    const el = makeEditor();
    let received = null;
    el.addEventListener('apply', e => { received = e.detail; });
    el.shadowRoot.querySelector('af-button.apply-btn').dispatchEvent(new Event('click'));
    expect(received).toEqual({ id: '01TPL', platform_id: 'youtube', topic: 'Tutorials' });
  });

  it('hides the archive button when status=archived', () => {
    const el = makeEditor({ status: 'archived' });
    expect(el.shadowRoot.querySelector('af-button.archive-btn')).toBeFalsy();
  });
});
