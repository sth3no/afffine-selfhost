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
