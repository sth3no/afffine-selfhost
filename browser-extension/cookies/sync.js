/**
 * Cookie sync subsystem — moved out of background.js for the v0.2 multitool
 * refactor. Behavior is unchanged from v0.1; only the imports and HTTP plumbing
 * have been swapped to the shared lib/* modules.
 *
 * See spec §6.2 (data flow) and the v0.1 README at portainer-stack/browser-extension/README.md.
 */
import { request, IngestError } from '../lib/api.js';
import { getConfig, getLastSync, setLastSync } from '../lib/storage.js';
import { setSubsystem } from '../lib/badge.js';
import { cookiesToNetscape } from './netscape.js';

const STALE_AFTER_SECONDS = 60 * 60 * 24;  // 24h — beyond this, "warn".

export async function collectYouTubeCookies() {
  const requests = [
    chrome.cookies.getAll({ domain: 'youtube.com' }),
    chrome.cookies.getAll({ domain: '.youtube.com' }),
  ];

  const { extendedScope } = await getConfig();
  if (extendedScope) {
    const hasPerm = await chrome.permissions.contains({
      origins: ['*://accounts.google.com/*'],
    });
    if (hasPerm) {
      requests.push(chrome.cookies.getAll({ domain: 'accounts.google.com' }));
      requests.push(chrome.cookies.getAll({ domain: '.google.com' }));
    }
  }

  const buckets = await Promise.all(requests);
  const seen = new Set();
  const out = [];
  for (const c of buckets.flat()) {
    const key = `${c.name}|${c.domain}|${c.path}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(c);
  }
  return out;
}

export async function fetchServerStatus() {
  try {
    return await request('GET', '/youtube/cookies/status');
  } catch (e) {
    return null;  // unknown — don't change badge
  }
}

export function verdictFromStatus(status) {
  if (!status) return 'unknown';
  if (!status.exists) return 'missing';
  if ((status.age_seconds ?? 0) >= STALE_AFTER_SECONDS) return 'stale';
  return 'fresh';
}

/**
 * Full sync flow. Persists `lastSync` so the popup can render without
 * re-running the work, and updates the cookie subsystem badge.
 */
export async function syncCookies() {
  const { ingestUrl, ingestToken } = await getConfig();
  if (!ingestUrl || !ingestToken) {
    const result = { ok: false, error: 'not configured', synced_at: null };
    await setLastSync(result);
    return result;
  }

  let cookies;
  try {
    cookies = await collectYouTubeCookies();
  } catch (e) {
    const result = { ok: false, error: `collect failed: ${e?.message ?? e}`, synced_at: null };
    await setLastSync(result);
    return result;
  }

  if (cookies.length === 0) {
    const result = { ok: false, error: 'no YouTube cookies — log into YouTube first', synced_at: null };
    await setLastSync(result);
    return result;
  }

  const body = cookiesToNetscape(cookies);
  let uploadOk = true;
  let uploadError = null;
  try {
    await request('POST', '/youtube/cookies', { body, bodyType: 'text' });
  } catch (e) {
    uploadOk = false;
    uploadError = e instanceof IngestError ? e.message : String(e);
  }

  const serverStatus = uploadOk ? await fetchServerStatus() : null;
  const verdict = verdictFromStatus(serverStatus);

  const result = {
    ok: uploadOk,
    cookie_count: cookies.length,
    byte_count: body.length,
    synced_at: new Date().toISOString(),
    error: uploadError,
    server_status: serverStatus,
    verdict,
  };
  await setLastSync(result);
  await setSubsystem('cookies', verdict === 'stale' || verdict === 'missing' ? 'warn' : 'ok');
  return result;
}
