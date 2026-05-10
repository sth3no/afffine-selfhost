/**
 * Capture flow side-effects: POST /capture, persist lastResult, prepend to
 * recentCaptures cache, update capture-subsystem badge.
 *
 * Pure function over the storage + badge modules — used by both the popup
 * 'capture' message handler (background.js) and the context-menu click
 * handler (capture/context-menu.js).
 */
import { captureUrl } from './client.js';
import { setLastResult, getRecentCaptures, setRecentCaptures } from '../lib/storage.js';
import { setSubsystem } from '../lib/badge.js';
import { IngestError } from '../lib/api.js';

/**
 * @param {{url?: string, source_app?: string|null, shared_title?: string,
 *           shared_text?: string}} payload
 * @returns {Promise<{ok: true, capture_id: string, doc_id: string,
 *           web_url: string, status: string, platform: string,
 *           initial_path: string, created_at: string} |
 *           {ok: false, error: {kind: string, message: string, status?: number}}>}
 */
export async function performCapture(payload) {
  try {
    const response = await captureUrl(payload);
    const result = { ok: true, ...response };
    await setLastResult(result);
    const recent = await getRecentCaptures();
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
