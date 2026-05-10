/**
 * Per-site URL canonicalizers. Pure functions: input URL string →
 * canonical capture URL (string) or null (no per-item URL detected).
 */

export function youtubeUrl(rawUrl) {
  let u;
  try { u = new URL(rawUrl); } catch { return null; }
  if (u.hostname === 'youtu.be') {
    const id = u.pathname.slice(1).split('/')[0];
    if (!id) return null;
    return `https://www.youtube.com/watch?v=${id}`;
  }
  if (!/(?:^|\.)youtube\.com$/.test(u.hostname)) return null;
  if (u.pathname !== '/watch') return null;
  const v = u.searchParams.get('v');
  if (!v) return null;
  return `https://www.youtube.com/watch?v=${v}`;
}

export function instagramUrl(rawUrl) {
  let u;
  try { u = new URL(rawUrl); } catch { return null; }
  if (!/(?:^|\.)instagram\.com$/.test(u.hostname)) return null;
  // Match /p/<id>/, /reel/<id>/, or /reels/<id>/. Normalize "reels" → "reel"
  // for canonical output. Reject /reels/audio/<id>/ (audio permalinks aren't reels).
  const m = u.pathname.match(/^\/(p|reel|reels)\/([^/]+)\/?/);
  if (!m) return null;
  if (m[1] === 'reels' && m[2] === 'audio') return null;
  const kind = m[1] === 'reels' ? 'reel' : m[1];
  return `https://www.instagram.com/${kind}/${m[2]}/`;
}

export function twitterUrl(rawUrl) {
  let u;
  try { u = new URL(rawUrl); } catch { return null; }
  if (u.hostname !== 'x.com' && u.hostname !== 'twitter.com'
      && u.hostname !== 'www.x.com' && u.hostname !== 'www.twitter.com') {
    return null;
  }
  const m = u.pathname.match(/^\/([^/]+)\/status\/(\d+)/);
  if (!m) return null;
  return `https://x.com/${m[1]}/status/${m[2]}`;
}

export function tiktokUrl(rawUrl) {
  let u;
  try { u = new URL(rawUrl); } catch { return null; }
  if (!/(?:^|\.)tiktok\.com$/.test(u.hostname)) return null;
  const m = u.pathname.match(/^\/(@[^/]+)\/video\/(\d+)\/?$/);
  if (!m) return null;
  return `https://www.tiktok.com/${m[1]}/video/${m[2]}`;
}

export function redditUrl(rawUrl) {
  let u;
  try { u = new URL(rawUrl); } catch { return null; }
  if (!/(?:^|\.)reddit\.com$/.test(u.hostname)) return null;
  const m = u.pathname.match(/^\/r\/([^/]+)\/comments\/([^/]+)(\/[^/]+)?\/?/);
  if (!m) return null;
  const slug = m[3] ?? '';
  return `https://www.reddit.com/r/${m[1]}/comments/${m[2]}${slug}/`;
}
