/** @vitest-environment node */
import { describe, it, expect } from 'vitest';
import { cookiesToNetscape } from '../netscape.js';

describe('cookies/netscape.cookiesToNetscape', () => {
  it('emits header + 7 tab-separated columns per cookie', () => {
    const out = cookiesToNetscape([{
      domain: '.youtube.com', path: '/', secure: true, session: false,
      expirationDate: 1700000000, name: 'SID', value: 'abc',
    }]);
    expect(out).toMatch(/^# Netscape HTTP Cookie File/);
    const dataLines = out.trim().split('\n').filter(l => !l.startsWith('#') && l);
    expect(dataLines).toHaveLength(1);
    expect(dataLines[0].split('\t')).toEqual([
      '.youtube.com', 'TRUE', '/', 'TRUE', '1700000000', 'SID', 'abc',
    ]);
  });

  it('uses "0" for session cookies', () => {
    const out = cookiesToNetscape([{
      domain: 'youtube.com', path: '/', secure: false, session: true,
      name: 'tmp', value: 'v',
    }]);
    expect(out).toMatch(/\t0\t/);
  });

  it('marks subdomain inclusion based on leading dot', () => {
    const out = cookiesToNetscape([
      { domain: '.youtube.com', path: '/', secure: false, session: true, name: 'a', value: '1' },
      { domain: 'youtube.com',  path: '/', secure: false, session: true, name: 'b', value: '2' },
    ]);
    const lines = out.trim().split('\n').filter(l => !l.startsWith('#') && l);
    expect(lines[0]).toContain('TRUE');
    expect(lines[1]).toMatch(/^youtube\.com\tFALSE/);
  });
});
