/** @vitest-environment node */
import { describe, it, expect } from 'vitest';
import { buildPayloadFromTab } from '../payload.js';

describe('capture/payload.buildPayloadFromTab', () => {
  const tab = { url: 'https://www.youtube.com/watch?v=abc', title: 'Some video' };

  it('popup capture uses tab URL + title + hostname source_app', () => {
    expect(buildPayloadFromTab(tab)).toEqual({
      url: 'https://www.youtube.com/watch?v=abc',
      source_app: 'www.youtube.com',
      shared_title: 'Some video',
    });
  });

  it('link capture (info.linkUrl) uses link URL, NOT page URL', () => {
    const info = { linkUrl: 'https://example.com/article', selectionText: undefined };
    expect(buildPayloadFromTab(tab, info)).toEqual({
      url: 'https://example.com/article',
      source_app: 'www.youtube.com',  // host page is still the source
      shared_title: 'Some video',
    });
  });

  it('selection capture (info.selectionText) sends shared_text + page URL', () => {
    const info = { selectionText: 'A great quote.', linkUrl: undefined };
    expect(buildPayloadFromTab(tab, info)).toEqual({
      url: 'https://www.youtube.com/watch?v=abc',
      source_app: 'www.youtube.com',
      shared_title: 'Some video',
      shared_text: 'A great quote.',
    });
  });

  it('image capture (info.srcUrl) uses image URL', () => {
    const info = { srcUrl: 'https://cdn.example.com/img.png' };
    expect(buildPayloadFromTab(tab, info)).toEqual({
      url: 'https://cdn.example.com/img.png',
      source_app: 'www.youtube.com',
      shared_title: 'Some video',
    });
  });

  it('omits undefined shared_title gracefully', () => {
    const stripped = { url: tab.url, title: undefined };
    const out = buildPayloadFromTab(stripped);
    expect(out.shared_title).toBeUndefined();
    expect(out.url).toBe(tab.url);
  });

  it('handles non-http tab URL (chrome://) by leaving source_app empty', () => {
    const internal = { url: 'chrome://newtab/', title: 'New tab' };
    expect(buildPayloadFromTab(internal)).toEqual({
      url: 'chrome://newtab/',
      source_app: null,
      shared_title: 'New tab',
    });
  });
});
