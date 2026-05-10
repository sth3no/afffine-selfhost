/** @vitest-environment node */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { request, IngestError } from '../api.js';
import * as storage from '../storage.js';

describe('lib/api.request', () => {
  beforeEach(() => {
    vi.spyOn(storage, 'getConfig').mockResolvedValue({
      ingestUrl: 'https://ingest.test',
      ingestToken: 'tok',
      extendedScope: false,
    });
    globalThis.fetch = vi.fn();
  });

  it('attaches Bearer auth and JSON content-type', async () => {
    fetch.mockResolvedValue(new Response(JSON.stringify({ok: true}),
      {status: 200, headers: {'content-type': 'application/json'}}));
    await request('GET', '/health');
    const [url, opts] = fetch.mock.calls[0];
    expect(url).toBe('https://ingest.test/health');
    expect(opts.headers.Authorization).toBe('Bearer tok');
  });

  it('strips trailing slash on ingestUrl', async () => {
    storage.getConfig.mockResolvedValue({
      ingestUrl: 'https://ingest.test/', ingestToken: 't', extendedScope: false,
    });
    fetch.mockResolvedValue(new Response('{}', {status: 200,
      headers: {'content-type': 'application/json'}}));
    await request('GET', '/health');
    expect(fetch.mock.calls[0][0]).toBe('https://ingest.test/health');
  });

  it('serializes JSON body', async () => {
    fetch.mockResolvedValue(new Response('{}', {status: 200,
      headers: {'content-type': 'application/json'}}));
    await request('POST', '/capture', { body: { url: 'https://x' } });
    const opts = fetch.mock.calls[0][1];
    expect(opts.headers['Content-Type']).toBe('application/json');
    expect(opts.body).toBe('{"url":"https://x"}');
  });

  it('passes raw body through when bodyType=text', async () => {
    fetch.mockResolvedValue(new Response('{}', {status: 200,
      headers: {'content-type': 'application/json'}}));
    await request('POST', '/youtube/cookies', { body: 'cookie\ttext', bodyType: 'text' });
    const opts = fetch.mock.calls[0][1];
    expect(opts.headers['Content-Type']).toBe('text/plain');
    expect(opts.body).toBe('cookie\ttext');
  });

  it('throws IngestError invalid_token on 401', async () => {
    fetch.mockResolvedValue(new Response(JSON.stringify({error: {code: 'INVALID_TOKEN'}}),
      {status: 401, headers: {'content-type': 'application/json'}}));
    await expect(request('GET', '/health')).rejects.toMatchObject({
      kind: 'invalid_token', status: 401,
    });
  });

  it('throws IngestError rate_limited on 429 with retryAfter', async () => {
    fetch.mockResolvedValue(new Response('{}', {
      status: 429, headers: {'content-type': 'application/json', 'retry-after': '30'},
    }));
    await expect(request('GET', '/health')).rejects.toMatchObject({
      kind: 'rate_limited', retryAfter: 30,
    });
  });

  it('throws IngestError server on 500', async () => {
    fetch.mockResolvedValue(new Response('{}', {status: 500,
      headers: {'content-type': 'application/json'}}));
    await expect(request('GET', '/health')).rejects.toMatchObject({ kind: 'server' });
  });

  it('throws IngestError network on fetch reject', async () => {
    fetch.mockRejectedValue(new TypeError('Failed to fetch'));
    await expect(request('GET', '/health')).rejects.toMatchObject({ kind: 'network' });
  });

  it('throws IngestError config when ingestUrl unset', async () => {
    storage.getConfig.mockResolvedValue({ingestUrl: null, ingestToken: null, extendedScope: false});
    await expect(request('GET', '/health')).rejects.toMatchObject({ kind: 'config' });
  });
});
