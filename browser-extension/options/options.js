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
import '../options/components/af-history-row.js';
import '../options/components/af-status-timeline.js';
import '../options/components/af-breadcrumb.js';
import { getConfig, setConfig, getRecentCaptures } from '../lib/storage.js';
import { health, IngestError } from '../lib/api.js';
import { listCaptures, getCapture, retryCapture, deleteCapture } from '../capture/client.js';

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
  const top = hash.split('/')[0];
  return VALID_TABS.includes(top) ? top : 'settings';
}

function currentDetailId() {
  const hash = window.location.hash.replace('#', '');
  const parts = hash.split('/');
  return parts[0] === 'history' && parts[1] ? parts[1] : null;
}

function routeFromHash() {
  const target = currentTab();
  for (const tab of $tabs) {
    tab.classList.toggle('active', tab.dataset.tab === target);
  }
  for (const panel of $panels) {
    panel.hidden = panel.dataset.panel !== target;
  }
  if (target === 'history') {
    renderHistoryView();
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

// ── History tab ─────────────────────────────────────────────────────────────

const $historyList = document.getElementById('historyList');
const $historyEmpty = document.getElementById('historyEmpty');
const $historyDetail = document.getElementById('historyDetail');

let _historyItems = [];
let _activeFilter = 'all';

document.querySelectorAll('.filter-pill').forEach(pill => {
  pill.addEventListener('click', () => {
    document.querySelectorAll('.filter-pill').forEach(p => p.classList.remove('active'));
    pill.classList.add('active');
    _activeFilter = pill.dataset.filter;
    renderHistoryList();
  });
});

async function renderHistoryView() {
  const detailId = currentDetailId();
  if (detailId) {
    await renderHistoryDetail(detailId);
    return;
  }
  $historyList.hidden = false;
  document.querySelector('.filter-pills').hidden = false;
  $historyDetail.hidden = true;
  await loadHistoryList();
}

async function loadHistoryList() {
  const cached = await getRecentCaptures();
  if (cached.length) {
    _historyItems = cached;
    renderHistoryList();
  }
  try {
    const page = await listCaptures({ limit: 50 });
    _historyItems = page?.items ?? [];
    renderHistoryList();
  } catch (e) {
    if (!cached.length) {
      $historyList.innerHTML = '';
      $historyEmpty.hidden = false;
    }
  }
}

function renderHistoryList() {
  const filtered = filterItems(_historyItems, _activeFilter);
  $historyList.innerHTML = '';
  if (filtered.length === 0) {
    $historyEmpty.hidden = false;
    return;
  }
  $historyEmpty.hidden = true;
  for (const item of filtered) {
    const row = document.createElement('af-history-row');
    row.data = item;
    row.addEventListener('open', e => {
      window.location.hash = `#history/${e.detail.capture_id}`;
    });
    row.addEventListener('retry', async e => {
      try {
        await retryCapture(e.detail.capture_id);
        await loadHistoryList();
      } catch (err) { showToast(err?.message ?? 'Retry failed'); }
    });
    row.addEventListener('delete', async e => {
      if (!confirm(`Delete capture "${e.detail.shared_title ?? e.detail.url ?? ''}"?`)) return;
      try {
        await deleteCapture(e.detail.capture_id);
        _historyItems = _historyItems.filter(i => i.capture_id !== e.detail.capture_id);
        renderHistoryList();
      } catch (err) { showToast(err?.message ?? 'Delete failed'); }
    });
    $historyList.appendChild(row);
  }
}

function filterItems(items, filter) {
  if (filter === 'all') return items;
  if (filter === 'done') return items.filter(i => i.status === 'done');
  if (filter === 'failed') return items.filter(i => i.status === 'failed');
  if (filter === 'in-progress') {
    return items.filter(i => ['queued', 'extracting', 'classifying', 'filing'].includes(i.status));
  }
  return items;
}

async function renderHistoryDetail(captureId) {
  $historyList.hidden = true;
  document.querySelector('.filter-pills').hidden = true;
  $historyEmpty.hidden = true;
  $historyDetail.hidden = false;

  $historyDetail.innerHTML = `<p class="hint">Loading…</p>`;
  let detail;
  try {
    detail = await getCapture(captureId);
  } catch (e) {
    $historyDetail.innerHTML = `
      <button class="back-btn" id="back">← Back</button>
      <p class="hint" style="color: var(--af-error)">Couldn't load: ${e?.message ?? e}</p>
    `;
    document.getElementById('back').addEventListener('click', () => { window.location.hash = '#history'; });
    return;
  }

  const title = detail.shared_title ?? detail.url ?? '(untitled)';
  const reasoning = detail.classifier_reasoning
    ? `<div class="reasoning">${escapeText(detail.classifier_reasoning)}</div>`
    : '';
  const errorBlock = detail.status === 'failed' && detail.error
    ? `<div class="error-block">${escapeText(detail.error)}</div>`
    : '';

  $historyDetail.innerHTML = `
    <button class="back-btn" id="back">← Back</button>
    <h2 class="title">${escapeText(title)}</h2>
    <a class="web-url" href="${escapeAttr(detail.web_url ?? '#')}" target="_blank" rel="noopener">${escapeText(detail.web_url ?? '')}</a>
    <af-status-timeline status="${escapeAttr(detail.status)}"></af-status-timeline>
    ${reasoning}
    ${detail.topic_path ? `<af-breadcrumb path="${escapeAttr(detail.topic_path)}"></af-breadcrumb>` : ''}
    ${errorBlock}
    <div class="detail-actions">
      <af-button id="open" variant="primary">Open in AFFiNE</af-button>
      ${detail.status !== 'done' ? `<af-button id="retry" variant="secondary">Retry</af-button>` : ''}
      <af-button id="delete" variant="ghost">Delete</af-button>
    </div>
  `;

  document.getElementById('back').addEventListener('click', () => { window.location.hash = '#history'; });
  document.getElementById('open').addEventListener('click', () => {
    if (detail.web_url) chrome.tabs.create({ url: detail.web_url });
  });
  document.getElementById('retry')?.addEventListener('click', async () => {
    try {
      await retryCapture(captureId);
      await renderHistoryDetail(captureId);
    } catch (e) { showToast(e?.message ?? 'Retry failed'); }
  });
  document.getElementById('delete').addEventListener('click', async () => {
    if (!confirm(`Delete capture "${title}"?`)) return;
    try {
      await deleteCapture(captureId);
      window.location.hash = '#history';
    } catch (e) { showToast(e?.message ?? 'Delete failed'); }
  });
}

function escapeText(s) {
  const div = document.createElement('div');
  div.textContent = String(s ?? '');
  return div.innerHTML;
}
function escapeAttr(s) {
  return String(s ?? '').replace(/"/g, '&quot;').replace(/&/g, '&amp;');
}
