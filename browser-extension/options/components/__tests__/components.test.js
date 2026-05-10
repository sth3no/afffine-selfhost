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
