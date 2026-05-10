/** @vitest-environment node */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { getConfig, setConfig, getLastSync, setLastSync,
         getLastResult, setLastResult, getRecentCaptures, setRecentCaptures }
  from '../storage.js';

describe('lib/storage', () => {
  beforeEach(() => {
    chrome.storage.local.get = vi.fn(async (keys) => ({}));
    chrome.storage.local.set = vi.fn(async () => {});
  });

  it('getConfig returns ingestUrl + ingestToken + extendedScope from local storage', async () => {
    chrome.storage.local.get = vi.fn(async () => ({
      ingestUrl: 'https://example.com',
      ingestToken: 'tok',
      extendedScope: true,
    }));
    expect(await getConfig()).toEqual({
      ingestUrl: 'https://example.com', ingestToken: 'tok', extendedScope: true,
    });
  });

  it('setConfig writes only provided keys (partial update)', async () => {
    await setConfig({ ingestUrl: 'https://x' });
    expect(chrome.storage.local.set).toHaveBeenCalledWith({ ingestUrl: 'https://x' });
  });

  it('getLastSync returns null if never synced', async () => {
    expect(await getLastSync()).toBeNull();
  });

  it('setRecentCaptures truncates to 50 entries', async () => {
    const items = Array.from({length: 75}, (_, i) => ({capture_id: String(i)}));
    await setRecentCaptures(items);
    const passed = chrome.storage.local.set.mock.calls[0][0].recentCaptures;
    expect(passed).toHaveLength(50);
    expect(passed[0].capture_id).toBe('0');  // keep newest first; caller sorts
  });

  it('getRecentCaptures returns [] if missing', async () => {
    expect(await getRecentCaptures()).toEqual([]);
  });

  it('getLastResult returns null if never set', async () => {
    expect(await getLastResult()).toBeNull();
  });
});
