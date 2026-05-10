/**
 * AFFiNE Capture — background service worker.
 *
 * Top-level dispatcher only. Cookie sync logic lives in cookies/sync.js;
 * capture logic will be added in Phase 2 (capture/*). This file's job is to
 * register the chrome.* listeners and route them to the right module.
 */
import { syncCookies } from './cookies/sync.js';
import { refreshBadge } from './lib/badge.js';
import { performCapture } from './capture/handler.js';

const ALARM_DAILY_SYNC = 'yt-cookie-daily-sync';
const ALARM_DEBOUNCE_SYNC = 'yt-cookie-debounce-sync';
const DEBOUNCE_MINUTES = 0.5;

// First-install flow: kick off a sync (no-op if not configured) and create
// the daily safety-net alarm.
chrome.runtime.onInstalled.addListener(() => {
  syncCookies();
  chrome.alarms.create(ALARM_DAILY_SYNC, { periodInMinutes: 60 * 24 });
});

// Restore badge state from storage when the worker wakes up.
chrome.runtime.onStartup.addListener(() => {
  refreshBadge();
});

// Debounced sync on YouTube cookie changes.
chrome.cookies.onChanged.addListener(({ cookie }) => {
  if (!cookie?.domain?.includes('youtube.com')) return;
  chrome.alarms.create(ALARM_DEBOUNCE_SYNC, { delayInMinutes: DEBOUNCE_MINUTES });
});

chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === ALARM_DAILY_SYNC || alarm.name === ALARM_DEBOUNCE_SYNC) {
    syncCookies();
  }
});

// Manual triggers from popup / options.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === 'sync-now') {
    syncCookies().then(sendResponse);
    return true;
  }
  if (msg?.type === 'get-last-sync') {
    import('./lib/storage.js').then(m => m.getLastSync()).then(sendResponse);
    return true;
  }
  if (msg?.type === 'capture') {
    performCapture(msg.payload).then(sendResponse);
    return true;
  }
});

