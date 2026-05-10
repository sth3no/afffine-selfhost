/**
 * Reddit content script. Per-post pill on hover.
 * Anchor: `shreddit-post` (new Reddit) or `[data-testid=post-container]`
 * (legacy fallback). Captures the comments permalink.
 */
import { redditUrl } from './_shared/canonicalizers.js';
import './_shared/pill.js';

const POST_SELECTORS = [
  'shreddit-post',
  '[data-testid=post-container]',
];
const PILL_FLAG = 'data-af-pill';
const HOVER_FLAG = 'data-af-hover';

const observer = new MutationObserver(scan);
observer.observe(document.body, { childList: true, subtree: true });
scan();

function scan() {
  for (const sel of POST_SELECTORS) {
    for (const post of document.querySelectorAll(sel)) {
      if (post.hasAttribute(HOVER_FLAG)) continue;
      post.setAttribute(HOVER_FLAG, '1');
      post.addEventListener('mouseenter', () => placePillIn(post));
    }
  }
}

function placePillIn(post) {
  if (post.querySelector(`af-pill[${PILL_FLAG}]`)) return;
  const permalink = post.getAttribute?.('permalink')
    ?? post.querySelector('a[href*="/comments/"]')?.getAttribute('href');
  if (!permalink) return;
  const absolute = permalink.startsWith('http')
    ? permalink
    : `https://www.reddit.com${permalink}`;
  const url = redditUrl(absolute);
  if (!url) return;
  const pill = document.createElement('af-pill');
  pill.setAttribute(PILL_FLAG, '1');
  pill.dataset.url = url;
  pill.dataset.source = 'reddit';
  pill.style.position = 'absolute';
  pill.style.top = '8px';
  pill.style.right = '8px';
  pill.style.zIndex = '999';
  post.style.position = post.style.position || 'relative';
  post.appendChild(pill);
}
