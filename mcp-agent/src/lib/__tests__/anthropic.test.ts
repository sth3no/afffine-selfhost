import { describe, it, expect } from 'vitest';
import { SYSTEM_PROMPT } from '../anthropic.js';

describe('SYSTEM_PROMPT', () => {
  it('mentions JSON output and minimum cluster size', () => {
    expect(SYSTEM_PROMPT).toContain('JSON');
    expect(SYSTEM_PROMPT).toContain('at least 3 docs');
    expect(SYSTEM_PROMPT).toContain('2–5');
  });
});
