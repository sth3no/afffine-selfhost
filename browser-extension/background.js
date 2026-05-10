/**
 * AFFiNE Capture — background service worker.
 *
 * Top-level dispatcher only. Cookie sync logic lives in cookies/sync.js;
 * capture logic will be added in Phase 2 (capture/*). This file's job is to
 * register the chrome.* listeners and route them to the right module.
 */
import { syncCookies } from './cookies/sync.js';
import { refreshBadge, setSubsystem } from './lib/badge.js';
import { captureUrl } from './capture/client.js';
import { setLastResult, getRecentCaptures, setRecentCaptures } from './lib/storage.js';
import { IngestError } from './lib/api.js';

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
    handleCapture(msg.payload).then(sendResponse);
    return true;
  }
});

/**
 * Capture flow: POST /capture, persist lastResult + prepend to recentCaptures
 * cache, update capture-subsystem badge.
 */
async function handleCapture(payload) {
  try {
    const response = await captureUrl(payload);
    const result = { ok: true, ...response };
    await setLastResult(result);
    const recent = await getRecentCaptures();
    // Prepend (newest first); setRecentCaptures truncates to 50.
    await setRecentCaptures([toRecentRow(response), ...recent]);
    await setSubsystem('capture', 'ok');
    return result;
  } catch (e) {
    const err = e instanceof IngestError
      ? { kind: e.kind, message: e.message, status: e.status }
      : { kind: 'unknown', message: String(e) };
    const result = { ok: false, error: err };
    await setLastResult(result);
    await setSubsystem('capture', err.kind === 'invalid_token' ? 'warn' : 'ok');
    return result;
  }
}

/**
 * Project the server response into the lighter shape we cache for History.
 */
function toRecentRow(response) {
  return {
    capture_id: response.capture_id,
    doc_id: response.doc_id,
    web_url: response.web_url,
    status: response.status,
    platform: response.platform,
    topic_path: response.initial_path,
    created_at: response.created_at,
  };
}
