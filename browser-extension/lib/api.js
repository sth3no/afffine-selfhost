/**
 * Single shared HTTP client for the ingest service. Used by:
 *   - cookies/sync.js  (POST /youtube/cookies, GET /youtube/cookies/status)
 *   - capture/client.js (POST /capture, GET /captures, etc. — added in Phase 2)
 *
 * Returns parsed JSON on 2xx; throws an IngestError on every non-2xx and on
 * network failure. The caller never sees a Response object.
 *
 * Errors are typed by `kind` so UI surfaces can map them consistently
 * (see spec §7).
 */
import { getConfig } from './storage.js';

const TIMEOUT_MS = 10_000;

export class IngestError extends Error {
  constructor({ kind, message, status, retryAfter }) {
    super(message ?? kind);
    this.kind = kind;
    this.status = status ?? null;
    this.retryAfter = retryAfter ?? null;
  }
}

/**
 * @param {'GET'|'POST'|'PUT'|'DELETE'} method
 * @param {string} path  — server-relative, e.g. "/capture"
 * @param {{body?: any, bodyType?: 'json'|'text', signal?: AbortSignal}} [opts]
 * @returns {Promise<any>}  — parsed JSON, or `null` for empty 204s
 */
export async function request(method, path, opts = {}) {
  const { ingestUrl, ingestToken } = await getConfig();
  if (!ingestUrl || !ingestToken) {
    throw new IngestError({ kind: 'config', message: 'Server URL / token not configured' });
  }

  const url = `${ingestUrl.replace(/\/$/, '')}${path}`;
  const headers = { 'Authorization': `Bearer ${ingestToken}` };
  let body;
  if (opts.body !== undefined) {
    if (opts.bodyType === 'text') {
      headers['Content-Type'] = 'text/plain';
      body = opts.body;
    } else {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(opts.body);
    }
  }

  let resp;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    resp = await fetch(url, {
      method, headers, body,
      signal: opts.signal ?? controller.signal,
    });
  } catch (e) {
    throw new IngestError({ kind: 'network', message: e?.message ?? String(e) });
  } finally {
    clearTimeout(timer);
  }

  if (resp.status === 401) {
    throw new IngestError({ kind: 'invalid_token', status: 401, message: 'Token rejected' });
  }
  if (resp.status === 429) {
    const retryAfter = Number(resp.headers.get('retry-after')) || null;
    throw new IngestError({ kind: 'rate_limited', status: 429, retryAfter,
      message: 'Rate limited' });
  }
  if (resp.status >= 500) {
    throw new IngestError({ kind: 'server', status: resp.status,
      message: `Server error ${resp.status}` });
  }
  if (!resp.ok) {
    throw new IngestError({ kind: 'server', status: resp.status,
      message: `HTTP ${resp.status}` });
  }
  if (resp.status === 204) return null;
  const ct = resp.headers.get('content-type') ?? '';
  if (ct.includes('application/json')) return await resp.json();
  return await resp.text();
}

/**
 * Convenience wrapper for the health probe. Returns
 * `{ok: bool, queue_depth: int, worker_alive: bool, version: string}`.
 * Throws IngestError on auth/network errors.
 */
export async function health() {
  return await request('GET', '/health');
}
