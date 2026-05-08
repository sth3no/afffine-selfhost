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
}

function formatRelative(date) {
  const sec = Math.floor((Date.now() - date.getTime()) / 1000);
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}
