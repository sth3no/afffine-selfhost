/**
 * AFFiNE Capture — toolbar popup.
 *
 * Mirrors the iOS Share Extension UI (spec §4.1):
 *   - shows current page (favicon + title)
 *   - primary "Save to AFFiNE" button
 *   - status row swaps in place: idle → capturing → saved (with web_url) | error
 *   - footer with cookie subsystem status + deep-link to options
 *
 * Auto-closes 2s after a successful capture (preserves v0.1 cookie sync UX).
 */
import { buildPayloadFromTab } from '../capture/payload.js';

const $favicon       = document.getElementById('favicon');
const $pageTitle     = document.getElementById('pageTitle');
const $captureBtn    = document.getElementById('captureBtn');
const $captureStatus = document.getElementById('captureStatus');
const $cookieStatus  = document.getElementById('cookieStatus');
const $openOptions   = document.getElementById('openOptions');

renderHeader();
renderCookieStatus();

$captureBtn.addEventListener('click', async () => {
  $captureBtn.disabled = true;
  setCaptureStatus('Capturing…', null);
  const tab = await getActiveTab();
  if (!tab) { $captureBtn.disabled = false; setCaptureStatus('No tab to capture', 'err'); return; }
  const payload = buildPayloadFromTab(tab);
  let result;
  try {
    result = await chrome.runtime.sendMessage({ type: 'capture', payload });
  } catch (e) {
    setCaptureStatus(`Failed: ${e?.message ?? e}`, 'err');
    $captureBtn.disabled = false;
    return;
  }
  if (result?.ok) {
    setCaptureStatusSaved(result.web_url);
    setTimeout(() => window.close(), 2000);
  } else {
    setCaptureStatusError(result?.error);
    $captureBtn.disabled = false;
  }
});

$openOptions.addEventListener('click', e => {
  e.preventDefault();
  if (chrome.runtime.openOptionsPage) chrome.runtime.openOptionsPage();
  else window.open(chrome.runtime.getURL('options/options.html'));
});

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab ?? null;
}

async function renderHeader() {
  const tab = await getActiveTab();
  if (!tab) { $pageTitle.textContent = 'No active tab'; return; }
  $pageTitle.textContent = tab.title || tab.url || 'Untitled';
  if (tab.favIconUrl) $favicon.src = tab.favIconUrl;
}

async function renderCookieStatus() {
  const lastSync = await chrome.runtime.sendMessage({ type: 'get-last-sync' });
  if (!lastSync) { $cookieStatus.textContent = 'Cookies: never synced'; return; }
  if (!lastSync.ok) {
    $cookieStatus.textContent = `Cookies: ${lastSync.error ?? 'failed'}`;
    $cookieStatus.classList.add('err');
    return;
  }
  const ago = formatRelative(new Date(lastSync.synced_at));
  $cookieStatus.textContent = `Cookies: synced ${ago}`;
  if (lastSync.verdict === 'stale') $cookieStatus.classList.add('warn');
  if (lastSync.verdict === 'missing') $cookieStatus.classList.add('err');
}

function setCaptureStatus(text, kind) {
  $captureStatus.hidden = false;
  $captureStatus.className = 'capture-status' + (kind ? ` ${kind}` : '');
  $captureStatus.textContent = text;
}

function setCaptureStatusSaved(webUrl) {
  $captureStatus.hidden = false;
  $captureStatus.className = 'capture-status ok';
  $captureStatus.textContent = '✓ Saved';
  if (webUrl) {
    const a = document.createElement('a');
    a.href = webUrl;
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = 'Open in AFFiNE ↗';
    a.className = 'open-link';
    $captureStatus.append(' ', a);
  }
}

function setCaptureStatusError(err) {
  $captureStatus.hidden = false;
  $captureStatus.className = 'capture-status err';
  if (err?.kind === 'invalid_token') {
    $captureStatus.textContent = 'Token rejected — open Settings';
  } else if (err?.kind === 'config') {
    $captureStatus.textContent = 'Not configured — open Settings';
  } else if (err?.kind === 'rate_limited') {
    $captureStatus.textContent = `Rate limited; try again in ${err.retryAfter ?? '?'}s`;
  } else if (err?.kind === 'network') {
    $captureStatus.textContent = `Couldn't reach ingest`;
  } else {
    $captureStatus.textContent = err?.message ?? 'Failed';
  }
}

function formatRelative(date) {
  const sec = Math.floor((Date.now() - date.getTime()) / 1000);
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}
