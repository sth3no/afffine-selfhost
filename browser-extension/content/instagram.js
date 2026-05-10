/**
 * Instagram content script. Injects an <af-pill> per post/reel article
 * on hover. Targets `article[role=presentation]` containers.
 */
import { instagramUrl } from './_shared/canonicalizers.js';
import './_shared/pill.js';

const POST_SELECTOR = 'article[role=presentation]';
const PILL_FLAG = 'data-af-pill';
const HOVER_FLAG = 'data-af-hover';

const observer = new MutationObserver(scan);
observer.observe(document.body, { childList: true, subtree: true });
scan();

function scan() {
  const posts = document.querySelectorAll(POST_SELECTOR);
  for (const post of posts) {
    if (post.hasAttribute(HOVER_FLAG)) continue;
    post.setAttribute(HOVER_FLAG, '1');
    post.addEventListener('mouseenter', () => placePillIn(post));
  }
}

function placePillIn(post) {
  if (post.querySelector(`af-pill[${PILL_FLAG}]`)) return;
  const link = post.querySelector('a[href*="/p/"], a[href*="/reel/"]');
  if (!link) return;
  const url = instagramUrl(link.href);
  if (!url) return;
  const pill = document.createElement('af-pill');
  pill.setAttribute(PILL_FLAG, '1');
  pill.dataset.url = url;
  pill.dataset.source = 'instagram';
  pill.style.position = 'absolute';
  pill.style.top = '8px';
  pill.style.right = '8px';
  pill.style.zIndex = '999';
  post.style.position = post.style.position || 'relative';
  post.appendChild(pill);
}
