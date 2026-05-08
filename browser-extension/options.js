/**
 * Options page — read/write extension config and trigger manual sync.
 */

const $url = document.getElementById('ingestUrl');
const $token = document.getElementById('ingestToken');
const $save = document.getElementById('save');
const $syncNow = document.getElementById('syncNow');
const $status = document.getElementById('status');

// Load existing values + last-sync status.
chrome.storage.local.get(['ingestUrl', 'ingestToken', 'lastSync']).then(data => {
  if (data.ingestUrl) $url.value = data.ingestUrl;
  if (data.ingestToken) $token.value = data.ingestToken;
  renderStatus(data.lastSync ?? null);
});

$save.addEventListener('click', async () => {
  const ingestUrl = $url.value.trim();
  const ingestToken = $token.value.trim();

  if (!ingestUrl) {
    setStatus('err', 'Ingest URL is required.');
    return;
  }
  // Reject HTTP unless it's localhost — extension cookies are sensitive.
  try {
    const u = new URL(ingestUrl);
    if (u.protocol !== 'https:' && u.hostname !== 'localhost' && u.hostname !== '127.0.0.1') {
      setStatus('err', 'Use HTTPS (or localhost). Plain HTTP is blocked for safety.');
      return;
    }
  } catch {
    setStatus('err', 'Ingest URL is not valid.');
    return;
  }
  if (!ingestToken) {
    setStatus('err', 'Bearer token is required.');
    return;
  }

  await chrome.storage.local.set({ ingestUrl, ingestToken });
  setStatus('ok', 'Saved. Click "Sync now" to push cookies immediately.');
});

$syncNow.addEventListener('click', async () => {
  $syncNow.disabled = true;
  $syncNow.textContent = 'Syncing…';
  try {
    const result = await chrome.runtime.sendMessage({ type: 'sync-now' });
    renderStatus(result);
  } catch (e) {
    setStatus('err', `Sync failed: ${e?.message ?? e}`);
  } finally {
    $syncNow.disabled = false;
    $syncNow.textContent = 'Sync now';
  }
});

function renderStatus(lastSync) {
  if (!lastSync) {
    setStatus('', 'Last sync: never.');
    return;
  }
  if (lastSync.ok) {
    const when = new Date(lastSync.synced_at).toLocaleString();
    setStatus(
      'ok',
      `Synced ${when}. ${lastSync.cookie_count} cookies, ${lastSync.byte_count} bytes.`,
    );
  } else {
    setStatus('err', `Sync failed: ${lastSync.error ?? 'unknown'}`);
  }
}

function setStatus(kind, msg) {
  $status.className = `status ${kind}`;
  $status.textContent = msg;
}
