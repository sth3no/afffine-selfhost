/** @vitest-environment node */
import { describe, it, expect } from 'vitest';
import {
  youtubeUrl, instagramUrl, twitterUrl, tiktokUrl, redditUrl,
} from '../_shared/canonicalizers.js';

describe('canonicalizers/youtubeUrl', () => {
  it('strips playlist + index params, keeps v=', () => {
    expect(youtubeUrl('https://www.youtube.com/watch?v=abc&list=foo&index=2'))
      .toBe('https://www.youtube.com/watch?v=abc');
  });
  it('strips timestamp t=', () => {
    expect(youtubeUrl('https://www.youtube.com/watch?v=abc&t=42'))
      .toBe('https://www.youtube.com/watch?v=abc');
  });
  it('handles youtu.be short URL', () => {
    expect(youtubeUrl('https://youtu.be/abc?si=xyz'))
      .toBe('https://www.youtube.com/watch?v=abc');
  });
  it('handles m.youtube.com', () => {
    expect(youtubeUrl('https://m.youtube.com/watch?v=abc'))
      .toBe('https://www.youtube.com/watch?v=abc');
  });
  it('returns null on no v=', () => {
    expect(youtubeUrl('https://www.youtube.com/feed/subscriptions')).toBeNull();
  });
});

describe('canonicalizers/instagramUrl', () => {
  it('post permalink', () => {
    expect(instagramUrl('https://www.instagram.com/p/AbC123/'))
      .toBe('https://www.instagram.com/p/AbC123/');
  });
  it('reel permalink', () => {
    expect(instagramUrl('https://www.instagram.com/reel/AbC123/'))
      .toBe('https://www.instagram.com/reel/AbC123/');
  });
  it('strips query string + utm', () => {
    expect(instagramUrl('https://www.instagram.com/p/AbC123/?utm_source=ig_web'))
      .toBe('https://www.instagram.com/p/AbC123/');
  });
  it('returns null for profile root', () => {
    expect(instagramUrl('https://www.instagram.com/some_user/')).toBeNull();
  });
  it('returns null for stories', () => {
    expect(instagramUrl('https://www.instagram.com/stories/some_user/123/')).toBeNull();
  });
  it('normalizes /reels/<id>/ → /reel/<id>/', () => {
    expect(instagramUrl('https://www.instagram.com/reels/AbC123/'))
      .toBe('https://www.instagram.com/reel/AbC123/');
  });
  it('returns null for /reels/audio/<id>/ (audio permalink)', () => {
    expect(instagramUrl('https://www.instagram.com/reels/audio/207009109985651/'))
      .toBeNull();
  });
});

describe('canonicalizers/twitterUrl', () => {
  it('extracts user + status from x.com', () => {
    expect(twitterUrl('https://x.com/elonmusk/status/12345'))
      .toBe('https://x.com/elonmusk/status/12345');
  });
  it('normalizes twitter.com → x.com', () => {
    expect(twitterUrl('https://twitter.com/jack/status/9999'))
      .toBe('https://x.com/jack/status/9999');
  });
  it('strips trailing /photo/1 or other paths', () => {
    expect(twitterUrl('https://x.com/foo/status/42/photo/1'))
      .toBe('https://x.com/foo/status/42');
  });
  it('strips query', () => {
    expect(twitterUrl('https://x.com/foo/status/42?s=20')).toBe('https://x.com/foo/status/42');
  });
  it('null on profile pages', () => {
    expect(twitterUrl('https://x.com/foo')).toBeNull();
  });
});

describe('canonicalizers/tiktokUrl', () => {
  it('extracts @user/video/id', () => {
    expect(tiktokUrl('https://www.tiktok.com/@user/video/123?some=q'))
      .toBe('https://www.tiktok.com/@user/video/123');
  });
  it('handles m.tiktok.com', () => {
    expect(tiktokUrl('https://m.tiktok.com/@user/video/123'))
      .toBe('https://www.tiktok.com/@user/video/123');
  });
  it('null on FYP feed', () => {
    expect(tiktokUrl('https://www.tiktok.com/foryou')).toBeNull();
  });
  it('null on user profile root', () => {
    expect(tiktokUrl('https://www.tiktok.com/@user')).toBeNull();
  });
  it('strips trailing slash', () => {
    expect(tiktokUrl('https://www.tiktok.com/@user/video/123/'))
      .toBe('https://www.tiktok.com/@user/video/123');
  });
});

describe('canonicalizers/redditUrl', () => {
  it('extracts /r/sub/comments/id/slug/', () => {
    expect(redditUrl('https://www.reddit.com/r/programming/comments/abc/some_post/'))
      .toBe('https://www.reddit.com/r/programming/comments/abc/some_post/');
  });
  it('handles old.reddit.com → www', () => {
    expect(redditUrl('https://old.reddit.com/r/programming/comments/abc/some_post/'))
      .toBe('https://www.reddit.com/r/programming/comments/abc/some_post/');
  });
  it('handles short comment links', () => {
    expect(redditUrl('https://www.reddit.com/r/programming/comments/abc/'))
      .toBe('https://www.reddit.com/r/programming/comments/abc/');
  });
  it('strips utm', () => {
    expect(redditUrl('https://www.reddit.com/r/programming/comments/abc/some_post/?utm_source=share'))
      .toBe('https://www.reddit.com/r/programming/comments/abc/some_post/');
  });
  it('null on subreddit root', () => {
    expect(redditUrl('https://www.reddit.com/r/programming/')).toBeNull();
  });
});
