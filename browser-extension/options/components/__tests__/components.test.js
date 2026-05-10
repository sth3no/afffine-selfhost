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
