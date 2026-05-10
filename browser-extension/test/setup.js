// Per-test files override individual chrome.* methods with vi.fn()s. This file
// just gives `globalThis.chrome` a baseline shape so importing modules don't
// throw on top-level `chrome.foo` accesses.
import { vi } from 'vitest';

globalThis.chrome = {
  storage: {
    local: {
      get: vi.fn(async () => ({})),
      set: vi.fn(async () => {}),
    },
  },
  cookies: {
    getAll: vi.fn(async () => []),
    onChanged: { addListener: vi.fn() },
  },
  alarms: {
    create: vi.fn(),
    onAlarm: { addListener: vi.fn() },
    getAll: vi.fn(async () => []),
  },
  runtime: {
    onInstalled: { addListener: vi.fn() },
    onMessage: { addListener: vi.fn() },
    onStartup: { addListener: vi.fn() },
  },
  action: {
    setBadgeText: vi.fn(async () => {}),
    setBadgeBackgroundColor: vi.fn(async () => {}),
  },
  permissions: {
    contains: vi.fn(async () => false),
    request: vi.fn(async () => true),
    remove: vi.fn(async () => true),
  },
};
