/**
 * Capture API client. Thin wrappers over lib/api.js for the five capture-side
 * endpoints:
 *
 *   POST   /capture
 *   GET    /captures?limit=&status=&cursor=
 *   GET    /captures/{id}
 *   POST   /captures/{id}/retry
 *   DELETE /captures/{id}
 *
 * Each method returns the parsed JSON; lib/api.js maps non-2xx into IngestError.
 */
import { request } from '../lib/api.js';

/**
 * @param {{url?: string, source_app?: string|null, shared_title?: string,
 *           shared_text?: string}} payload
 * @returns {Promise<{capture_id: string, doc_id: string, web_url: string,
 *           status: string, platform: string, initial_path: string,
 *           created_at: string}>}
 */
export async function captureUrl(payload) {
  return await request('POST', '/capture', { body: payload });
}

export async function listCaptures({ limit = 50, status, platform, cursor } = {}) {
  const params = new URLSearchParams();
  params.set('limit', String(limit));
  if (status) params.set('status', status);
  if (platform) params.set('platform', platform);
  if (cursor) params.set('cursor', cursor);
  return await request('GET', `/captures?${params}`);
}

export async function getCapture(id) {
  return await request('GET', `/captures/${encodeURIComponent(id)}`);
}

export async function retryCapture(id) {
  return await request('POST', `/captures/${encodeURIComponent(id)}/retry`);
}

export async function deleteCapture(id) {
  return await request('DELETE', `/captures/${encodeURIComponent(id)}`);
}

/**
 * Re-renders a capture with the currently-resolved template against its
 * stored `extracted_snapshot`. Replaces the doc body in AFFiNE (v1 is
 * append-only — see docs/api-for-extension.md §3.8).
 *
 * @param {string} id  capture id
 * @param {{reextract?: boolean}} [opts]
 * @returns {Promise<object>}  the updated CaptureDetail JSON
 */
export async function rerenderCapture(id, { reextract = false } = {}) {
  const q = reextract ? '?reextract=true' : '';
  return await request('POST', `/captures/${encodeURIComponent(id)}/rerender${q}`);
}
