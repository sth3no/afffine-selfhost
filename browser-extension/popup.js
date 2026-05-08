/**
 * Toolbar popup — show last sync + trigger manual sync.
 */

const $status = document.getElementById('status');
const $syncNow = document.getElementById('syncNow');
const $openOptions = document.getElementById('openOptions');

renderLastSync();

$syncNow.addEventListener('click', async () => {
  $syncNow.disabled = true;
  $syncNow.textContent = 'Syncing…';
  try {
    const result = await chrome.runtime.sendMessage({ type: 'sync-now' });
    setStatus(result);
    if (result?.ok) {
      // Auto-close 2s after success.
      setTimeout(() => window.close(), 2000);
    }
  } catch (e) {
    $status.className = 'status err';
    $status.textContent = `Failed: ${e?.message ?? e}`;
  } finally {
    $syncNow.disabled = false;
    $syncNow.textContent = 'Sync now';
  }
});

$openOptions.addEventListener('click', () => {
  if (chrome.runtime.openOptionsPage) {
    chrome.runtime.openOptionsPage();
  } else {
    window.open(chrome.runtime.getURL('options.html'));
  }
});

async function renderLastSync() {
  const result = await chrome.runtime.sendMessage({ type: 'get-last-sync' });
  setStatus(result);
}

function setStatus(result) {
  if (!result) {
    $status.className = 'status';
    $status.textContent = 'Last sync: never. Click Settings to configure.';
    setServerStatus(null);
    return;
  }
  if (result.ok) {
    const when = formatRelative(new Date(result.synced_at));
    $status.className = 'status ok';
    $status.textContent = `Synced ${when} — ${result.cookie_count} cookies.`;
  } else {
    $status.className = 'status err';
    $status.textContent = `Failed: ${result.error ?? 'unknown'}`;
  }
  setServerStatus(result);
}

// Server-side verdict (phase 12.5). Disagrees with the line above when
// the ingest container has restarted and lost the tmpfs cookies file —
// browser thinks it synced, server has nothing.
function setServerStatus(result) {
  const $sv = document.getElementById('serverStatus');
  if (!result || !result.verdict || result.verdict === 'unknown') {
    $sv.hidden = true;
    return;
  }
  $sv.hidden = false;
  if (result.verdict === 'fresh') {
    $sv.className = 'status server';
    const ageMin = Math.floor((result.server_status?.age_seconds ?? 0) / 60);
    $sv.textContent = `Server: fresh (uploaded ${ageMin} min ago)`;
  } else if (result.verdict === 'stale') {
    $sv.className = 'status server warn';
    const ageH = Math.floor((result.server_status?.age_seconds ?? 0) / 3600);
    $sv.textContent = `Server: stale (cookies ${ageH}h old). Open YouTube + click Sync now.`;
  } else if (result.verdict === 'missing') {
    $sv.className = 'status server err';
    $sv.textContent = `Server: cookies missing. Likely an ingest restart — click Sync now.`;
  }
}

function formatRelative(date) {
  const sec = Math.floor((Date.now() - date.getTime()) / 1000);
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}
