/**
 * AFFiNE Capture — options page.
 *
 * Four tabs: Settings · History · Cookies · Templates.
 * URL hash routes drive tab visibility (#settings, #history, #cookies, #templates,
 * plus #history/<id> and #templates/<id> deep links). The Settings tab persists
 * URL + token to chrome.storage.local via lib/storage.js; History wraps the
 * capture API (lib/api.js + capture/client.js); Templates wraps templates/client.js
 * and composes <af-template-row> / <af-template-editor>.
 */
import '../options/components/af-button.js';
import '../options/components/af-input.js';
import '../options/components/af-card.js';
import '../options/components/af-history-row.js';
import '../options/components/af-status-timeline.js';
import '../options/components/af-breadcrumb.js';
import '../options/components/af-template-row.js';
import '../options/components/af-template-editor.js';
import '../options/components/af-prompt-textarea.js';
import { getConfig, setConfig, getRecentCaptures, getLastSync } from '../lib/storage.js';
import { health, IngestError } from '../lib/api.js';
import { listCaptures, getCapture, retryCapture, deleteCapture, rerenderCapture } from '../capture/client.js';
import {
  listTemplates,
  getTemplate,
  createTemplate,
  updateTemplate,
  archiveTemplate,
  synthesizeTemplate,
} from '../templates/client.js';

const VALID_TABS = ['settings', 'history', 'cookies', 'templates'];

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

function currentDetailId(forTab) {
  const hash = window.location.hash.replace('#', '');
  const parts = hash.split('/');
  return parts[0] === forTab && parts[1] ? parts[1] : null;
}

function routeFromHash() {
  const target = currentTab();
  for (const tab of $tabs) {
    tab.classList.toggle('active', tab.dataset.tab === target);
  }
  for (const panel of $panels) {
    panel.hidden = panel.dataset.panel !== target;
  }
  if (target === 'history') renderHistoryView();
  if (target === 'cookies') renderCookiesView();
  if (target === 'templates') renderTemplatesView();
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
  const detailId = currentDetailId('history');
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

// ── Cookies tab ──────────────────────────────────────────────────────────────

const $cookieLastSync = document.getElementById('cookieLastSync');
const $cookieServerStatus = document.getElementById('cookieServerStatus');
const $syncNow = document.getElementById('syncNow');
const $syncResult = document.getElementById('syncResult');
const $extendedScope = document.getElementById('extendedScope');

$syncNow.addEventListener('click', async () => {
  $syncResult.hidden = false;
  $syncResult.className = 'test-result';
  $syncResult.textContent = 'Syncing…';
  const result = await chrome.runtime.sendMessage({ type: 'sync-now' });
  if (result?.ok) {
    $syncResult.classList.add('ok');
    $syncResult.textContent = `Synced — ${result.cookie_count} cookies`;
  } else {
    $syncResult.classList.add('err');
    $syncResult.textContent = `Failed: ${result?.error ?? 'unknown'}`;
  }
  await renderCookiesView();
});

$extendedScope.addEventListener('change', async () => {
  if ($extendedScope.checked) {
    const granted = await chrome.permissions.request({
      origins: ['*://accounts.google.com/*', '*://*.google.com/*'],
    });
    if (!granted) {
      $extendedScope.checked = false;
      return;
    }
    await setConfig({ extendedScope: true });
  } else {
    await chrome.permissions.remove({
      origins: ['*://accounts.google.com/*', '*://*.google.com/*'],
    });
    await setConfig({ extendedScope: false });
  }
});

async function renderCookiesView() {
  const lastSync = await getLastSync();
  if (!lastSync) {
    $cookieLastSync.textContent = 'never';
    $cookieServerStatus.textContent = 'unknown';
    $cookieServerStatus.className = 'cookie-verdict';
  } else if (!lastSync.ok) {
    $cookieLastSync.textContent = 'failed';
    $cookieServerStatus.textContent = lastSync.error ?? 'failed';
    $cookieServerStatus.className = 'cookie-verdict error';
  } else {
    const ago = formatRelativeOptions(new Date(lastSync.synced_at));
    $cookieLastSync.textContent = `${ago} (${lastSync.cookie_count} cookies)`;
    const verdict = lastSync.verdict ?? 'unknown';
    $cookieServerStatus.textContent = verdictLabel(verdict, lastSync.server_status);
    $cookieServerStatus.className = `cookie-verdict ${verdict}`;
  }
  const cfg = await getConfig();
  $extendedScope.checked = !!cfg.extendedScope;
}

function verdictLabel(verdict, serverStatus) {
  if (verdict === 'fresh') {
    const ageMin = Math.floor((serverStatus?.age_seconds ?? 0) / 60);
    return `fresh (uploaded ${ageMin} min ago)`;
  }
  if (verdict === 'stale') {
    const ageH = Math.floor((serverStatus?.age_seconds ?? 0) / 3600);
    return `stale (cookies ${ageH}h old)`;
  }
  if (verdict === 'missing') return 'cookies missing on server';
  return 'unknown';
}

function formatRelativeOptions(date) {
  const sec = Math.floor((Date.now() - date.getTime()) / 1000);
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

// ── Templates tab ────────────────────────────────────────────────────────────

const $tplPlatform = document.getElementById('tplPlatform');
const $tplTopic = document.getElementById('tplTopic');
const $tplStatus = document.getElementById('tplStatus');
const $tplRefresh = document.getElementById('tplRefresh');
const $tplNew = document.getElementById('tplNew');
const $templatesList = document.getElementById('templatesList');
const $templatesEmpty = document.getElementById('templatesEmpty');
const $templatesDetail = document.getElementById('templatesDetail');
const $templatesView = document.getElementById('templatesView');

let _templates = [];
let _platformOptions = new Set();

$tplRefresh.addEventListener('click', () => loadTemplatesList());
$tplPlatform.addEventListener('change', () => loadTemplatesList());
$tplStatus.addEventListener('change', () => loadTemplatesList());
$tplTopic.addEventListener('input', debounce(() => loadTemplatesList(), 250));
$tplNew.addEventListener('click', () => newTemplateFlow());

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

async function renderTemplatesView() {
  const detailId = currentDetailId('templates');
  if (detailId) {
    await renderTemplateDetail(detailId);
    return;
  }
  $templatesView.hidden = false;
  $templatesDetail.hidden = true;
  await loadTemplatesList();
}

async function loadTemplatesList() {
  const filter = {
    platform: $tplPlatform.value || undefined,
    topic: $tplTopic.value.trim() || undefined,
    statusFilter: $tplStatus.value || undefined,
  };
  try {
    _templates = await listTemplates(filter) ?? [];
  } catch (e) {
    showToast(errorLabel(e) || 'Couldn\'t load templates');
    _templates = [];
  }
  refreshPlatformOptions(_templates);
  renderTemplatesList();
}

function refreshPlatformOptions(items) {
  for (const t of items) _platformOptions.add(t.platform_id);
  const current = $tplPlatform.value;
  $tplPlatform.innerHTML = `<option value="">All</option>`
    + [..._platformOptions].sort().map(p =>
      `<option value="${escapeAttr(p)}"${p === current ? ' selected' : ''}>${escapeText(p)}</option>`,
    ).join('');
}

function renderTemplatesList() {
  $templatesList.innerHTML = '';
  if (_templates.length === 0) {
    $templatesEmpty.hidden = false;
    return;
  }
  $templatesEmpty.hidden = true;
  // Sort: most-used first, then alphabetic by scope.
  const sorted = [..._templates].sort((a, b) => {
    if ((b.usage_count ?? 0) !== (a.usage_count ?? 0)) {
      return (b.usage_count ?? 0) - (a.usage_count ?? 0);
    }
    return `${a.platform_id}/${a.topic}`.localeCompare(`${b.platform_id}/${b.topic}`);
  });
  for (const t of sorted) {
    const row = document.createElement('af-template-row');
    row.data = t;
    row.addEventListener('open', e => {
      window.location.hash = `#templates/${e.detail.id}`;
    });
    row.addEventListener('archive', e => archiveFlow(e.detail));
    $templatesList.appendChild(row);
  }
}

async function renderTemplateDetail(id) {
  $templatesView.hidden = true;
  $templatesDetail.hidden = false;
  $templatesDetail.innerHTML = `<p class="hint">Loading…</p>`;

  let detail;
  try {
    detail = await getTemplate(id);
  } catch (e) {
    $templatesDetail.innerHTML = `
      <button class="back-btn" id="tplBack">← Back</button>
      <p class="hint" style="color: var(--af-error)">Couldn't load: ${escapeText(e?.message ?? String(e))}</p>
    `;
    document.getElementById('tplBack').addEventListener('click', () => {
      window.location.hash = '#templates';
    });
    return;
  }

  $templatesDetail.innerHTML = `<af-template-editor></af-template-editor>`;
  const editor = $templatesDetail.querySelector('af-template-editor');
  editor.data = detail;
  editor.addEventListener('back', () => { window.location.hash = '#templates'; });
  editor.addEventListener('save', e => saveFlow(e.detail));
  editor.addEventListener('archive', e => archiveFlow(e.detail));
  editor.addEventListener('resynth', e => resynthFlow(e.detail));
  editor.addEventListener('apply', e => applyToCaptureFlow(e.detail));
}

async function saveFlow({ id, patch }) {
  try {
    await updateTemplate(id, patch);
    showToast('Saved');
    await renderTemplateDetail(id);
  } catch (e) {
    showToast(errorLabel(e) || 'Save failed');
  }
}

async function archiveFlow({ id, name }) {
  if (!confirm(`Archive template "${name}"? Existing captures keep their template_id reference but no future captures will use this template.`)) {
    return;
  }
  try {
    await archiveTemplate(id);
    showToast('Archived');
    window.location.hash = '#templates';
    await loadTemplatesList();
  } catch (e) {
    showToast(errorLabel(e) || 'Archive failed');
  }
}

async function resynthFlow({ id, platform_id, topic }) {
  if (!confirm(`Re-synthesize the template for (${platform_id}, ${topic})? This archives the current one and runs Sonnet 4.6 to generate a fresh template from the most recent capture.`)) {
    return;
  }
  let archived = false;
  try {
    await archiveTemplate(id);
    archived = true;
    const fresh = await synthesizeTemplate({ platformId: platform_id, topic });
    showToast('Re-synthesized');
    window.location.hash = `#templates/${fresh.id}`;
  } catch (e) {
    const base = errorLabel(e) || 'Re-synth failed';
    showToast(archived
      ? `${base} — old template is archived; use "New template…" to recreate`
      : base);
  }
}

async function applyToCaptureFlow({ platform_id, topic }) {
  // Implemented in Task 10.
  await applyToExistingCapturePicker({ platform_id, topic });
}

async function newTemplateFlow() {
  const platform_id = prompt('Platform id (e.g. youtube, instagram, * for wildcard):');
  if (!platform_id) return;
  const topic = prompt('Topic (e.g. Tutorials, Recipes, * for wildcard):');
  if (!topic) return;
  const name = prompt('Template name:', `${platform_id} · ${topic}`);
  if (!name) return;
  const system_prompt = prompt('Paste the system prompt (or "synth" to synthesize from a recent capture instead):');
  if (!system_prompt) return;

  try {
    let created;
    if (system_prompt === 'synth') {
      created = await synthesizeTemplate({ platformId: platform_id, topic });
    } else {
      created = await createTemplate({ platform_id, topic, name, system_prompt });
    }
    showToast('Created');
    window.location.hash = `#templates/${created.id}`;
  } catch (e) {
    showToast(errorLabel(e) || 'Create failed');
  }
}

// Stub: Task 10 wires this up.
async function applyToExistingCapturePicker(_filter) {
  showToast('Apply-to-capture flow comes in Task 10');
}
