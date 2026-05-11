/**
 * Templates API client — thin wrappers over lib/api.js for Phase 14 templates
 * endpoints:
 *
 *   GET    /templates
 *   GET    /templates/resolve
 *   GET    /templates/{id}
 *   POST   /templates
 *   PUT    /templates/{id}
 *   DELETE /templates/{id}
 *   POST   /templates/synthesize
 *
 * Each method returns parsed JSON; lib/api.js maps non-2xx into IngestError.
 */
import { request } from '../lib/api.js';

export async function listTemplates({ platform, topic, statusFilter } = {}) {
  const params = new URLSearchParams();
  if (platform) params.set('platform', platform);
  if (topic) params.set('topic', topic);
  if (statusFilter) params.set('status_filter', statusFilter);
  return await request('GET', `/templates?${params}`);
}

export async function getTemplate(id) {
  return await request('GET', `/templates/${encodeURIComponent(id)}`);
}

export async function resolveTemplate({ platform, topic }) {
  const params = new URLSearchParams({ platform, topic });
  return await request('GET', `/templates/resolve?${params}`);
}

export async function createTemplate(body) {
  return await request('POST', '/templates', { body });
}

export async function updateTemplate(id, patch) {
  return await request('PUT', `/templates/${encodeURIComponent(id)}`, { body: patch });
}

export async function archiveTemplate(id) {
  return await request('DELETE', `/templates/${encodeURIComponent(id)}`);
}

export async function synthesizeTemplate({ platformId, topic, sampleCaptureId } = {}) {
  const body = { platform_id: platformId, topic };
  if (sampleCaptureId !== undefined && sampleCaptureId !== null) {
    body.sample_capture_id = sampleCaptureId;
  }
  return await request('POST', '/templates/synthesize', { body });
}
