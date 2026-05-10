/**
 * AFFiNE Capture — options page.
 *
 * Three tabs: Settings (this phase) · History (Phase 6) · Cookies (Phase 7).
 * URL hash routes drive tab visibility. The Settings tab persists URL + token
 * to chrome.storage.local via lib/storage.js, and Test connection hits the
 * health endpoint via lib/api.js.
 */
import '../options/components/af-button.js';
import '../options/components/af-input.js';
import '../options/components/af-card.js';
import { getConfig, setConfig } from '../lib/storage.js';
import { health, IngestError } from '../lib/api.js';

const VALID_TABS = ['settings', 'history', 'cookies'];

const $tabs = document.querySelectorAll('.tab');
const $panels = document.querySelectorAll('.panel');
const $url = document.getElementById('ingestUrl');
const $token = document.getElementById('ingestToken');
const $test = document.getElementById('testConnection');
const $save = document.getElementById('save');
const $testResult = document.getElementById('testResult');

routeFromHash();
window.addEventListener('hashchange', routeFromHash);

loadSettings();

$test.addEventListener('click', testConnection);
$save.addEventListener('click', saveSettings);

function currentTab() {
  const hash = window.location.hash.replace('#', '');
  return VALID_TABS.includes(hash) ? hash : 'settings';
}

function routeFromHash() {
  const target = currentTab();
  for (const tab of $tabs) {
    tab.classList.toggle('active', tab.dataset.tab === target);
  }
  for (const panel of $panels) {
    panel.hidden = panel.dataset.panel !== target;
  }
}

async function loadSettings() {
  const cfg = await getConfig();
  // Wait a microtask so the Web Components have wired their value setters.
  await new Promise(r => requestAnimationFrame(r));
  $url.value = cfg.ingestUrl ?? '';
  $token.value = cfg.ingestToken ?? '';
}

async function testConnection() {
  $testResult.hidden = false;
  $testResult.className = 'test-result';
  $testResult.textContent = 'Testing…';
  // Save current values first so health() reads them via storage.
  await setConfig({ ingestUrl: $url.value.trim(), ingestToken: $token.value.trim() });
  try {
    const res = await health();
    if (res?.ok) {
      $testResult.classList.add('ok');
      $testResult.textContent = `OK · v${res.version ?? '?'} · queue ${res.queue_depth ?? 0}`;
    } else {
      $testResult.classList.add('err');
      $testResult.textContent = 'Server replied not-ok';
    }
  } catch (e) {
    $testResult.classList.add('err');
    if (e instanceof IngestError) {
      $testResult.textContent = errorLabel(e);
    } else {
      $testResult.textContent = e?.message ?? 'Failed';
    }
  }
}

async function saveSettings() {
  const url = $url.value.trim();
  const token = $token.value.trim();
  await setConfig({ ingestUrl: url || null, ingestToken: token || null });
  showToast('Saved');
}

function errorLabel(err) {
  switch (err.kind) {
    case 'invalid_token':  return 'Token rejected';
    case 'config':         return 'URL / token not configured';
    case 'rate_limited':   return `Rate limited (${err.retryAfter ?? '?'}s)`;
    case 'network':        return `Couldn't reach server`;
    case 'server':         return `Server error ${err.status ?? ''}`.trim();
    default:               return err.message ?? 'Failed';
  }
}

function showToast(text) {
  let toast = document.querySelector('.toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.className = 'toast';
    document.body.appendChild(toast);
  }
  toast.textContent = text;
  toast.classList.add('visible');
  setTimeout(() => toast.classList.remove('visible'), 2000);
}
