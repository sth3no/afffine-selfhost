/** @vitest-environment node */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import * as storage from '../../lib/storage.js';
import {
  listTemplates,
  getTemplate,
  resolveTemplate,
  createTemplate,
  updateTemplate,
  archiveTemplate,
  synthesizeTemplate,
} from '../client.js';

beforeEach(() => {
  vi.spyOn(storage, 'getConfig').mockResolvedValue({
    ingestUrl: 'https://ingest.test',
    ingestToken: 'tok',
    extendedScope: false,
  });
  globalThis.fetch = vi.fn().mockResolvedValue(
    new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } }),
  );
});

describe('templates/client', () => {
  it('listTemplates passes no params when called bare', async () => {
    await listTemplates();
    expect(fetch.mock.calls[0][0]).toBe('https://ingest.test/templates?');
    expect(fetch.mock.calls[0][1].method).toBe('GET');
  });

  it('listTemplates passes platform / topic / statusFilter as query params', async () => {
    await listTemplates({ platform: 'youtube', topic: 'Tutorials', statusFilter: 'edited' });
    const url = fetch.mock.calls[0][0];
    expect(url).toContain('platform=youtube');
    expect(url).toContain('topic=Tutorials');
    expect(url).toContain('status_filter=edited');
  });

  it('getTemplate uses GET /templates/{id} and URL-encodes', async () => {
    await getTemplate('01J7/AB');
    expect(fetch.mock.calls[0][0]).toBe('https://ingest.test/templates/01J7%2FAB');
  });

  it('resolveTemplate hits /templates/resolve with platform+topic', async () => {
    await resolveTemplate({ platform: 'youtube', topic: 'Tutorials' });
    expect(fetch.mock.calls[0][0]).toBe(
      'https://ingest.test/templates/resolve?platform=youtube&topic=Tutorials',
    );
  });

  it('createTemplate POSTs JSON body', async () => {
    await createTemplate({
      platform_id: 'youtube',
      topic: 'Recipes',
      name: 'YT Recipe v1',
      system_prompt: 'You are…',
    });
    const [url, opts] = fetch.mock.calls[0];
    expect(url).toBe('https://ingest.test/templates');
    expect(opts.method).toBe('POST');
    expect(JSON.parse(opts.body)).toEqual({
      platform_id: 'youtube',
      topic: 'Recipes',
      name: 'YT Recipe v1',
      system_prompt: 'You are…',
    });
  });

  it('updateTemplate PUTs a partial body', async () => {
    await updateTemplate('01J7AB', { name: 'New name' });
    const [url, opts] = fetch.mock.calls[0];
    expect(url).toBe('https://ingest.test/templates/01J7AB');
    expect(opts.method).toBe('PUT');
    expect(JSON.parse(opts.body)).toEqual({ name: 'New name' });
  });

  it('archiveTemplate DELETEs by id', async () => {
    await archiveTemplate('01J7AB');
    const [url, opts] = fetch.mock.calls[0];
    expect(url).toBe('https://ingest.test/templates/01J7AB');
    expect(opts.method).toBe('DELETE');
  });

  it('synthesizeTemplate POSTs platform_id/topic/sample_capture_id', async () => {
    await synthesizeTemplate({
      platformId: 'youtube',
      topic: 'Documentary',
      sampleCaptureId: '01J7XYZ',
    });
    const [url, opts] = fetch.mock.calls[0];
    expect(url).toBe('https://ingest.test/templates/synthesize');
    expect(opts.method).toBe('POST');
    expect(JSON.parse(opts.body)).toEqual({
      platform_id: 'youtube',
      topic: 'Documentary',
      sample_capture_id: '01J7XYZ',
    });
  });

  it('synthesizeTemplate omits sample_capture_id when undefined', async () => {
    await synthesizeTemplate({ platformId: 'youtube', topic: 'Documentary' });
    const body = JSON.parse(fetch.mock.calls[0][1].body);
    expect(body).toEqual({ platform_id: 'youtube', topic: 'Documentary' });
    expect('sample_capture_id' in body).toBe(false);
  });
});
