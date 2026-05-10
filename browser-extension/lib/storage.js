/**
 * Typed chrome.storage.local helpers. The only file in the extension that
 * directly reads/writes storage keys; everything else routes through here so
 * key naming stays consistent.
 *
 * Schema (see spec §6.3):
 *   ingestUrl       - string
 *   ingestToken     - string
 *   extendedScope   - bool
 *   lastSync        - object  (cookie subsystem result)
 *   lastResult      - object  (last capture result)
 *   recentCaptures  - array (<= 50 capture rows for instant History render)
 */

const RECENT_MAX = 50;

export async function getConfig() {
  const { ingestUrl, ingestToken, extendedScope } =
    await chrome.storage.local.get(['ingestUrl', 'ingestToken', 'extendedScope']);
  return {
    ingestUrl: ingestUrl ?? null,
    ingestToken: ingestToken ?? null,
    extendedScope: !!extendedScope,
  };
}

export async function setConfig(patch) {
  await chrome.storage.local.set(patch);
}

export async function getLastSync() {
  const { lastSync } = await chrome.storage.local.get('lastSync');
  return lastSync ?? null;
}

export async function setLastSync(value) {
  await chrome.storage.local.set({ lastSync: value });
}

export async function getLastResult() {
  const { lastResult } = await chrome.storage.local.get('lastResult');
  return lastResult ?? null;
}

export async function setLastResult(value) {
  await chrome.storage.local.set({ lastResult: value });
}

export async function getRecentCaptures() {
  const { recentCaptures } = await chrome.storage.local.get('recentCaptures');
  return recentCaptures ?? [];
}

export async function setRecentCaptures(items) {
  const capped = (items ?? []).slice(0, RECENT_MAX);
  await chrome.storage.local.set({ recentCaptures: capped });
}
