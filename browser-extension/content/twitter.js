/**
 * Twitter/X content script. Per-tweet pill on hover.
 * Anchor: `article[data-testid=tweet]`. Captures /<user>/status/<id>.
 */
import { twitterUrl } from './_shared/canonicalizers.js';
import './_shared/pill.js';

const TWEET_SELECTOR = 'article[data-testid=tweet]';
const PILL_FLAG = 'data-af-pill';
const HOVER_FLAG = 'data-af-hover';

const observer = new MutationObserver(scan);
observer.observe(document.body, { childList: true, subtree: true });
scan();

function scan() {
  for (const tweet of document.querySelectorAll(TWEET_SELECTOR)) {
    if (tweet.hasAttribute(HOVER_FLAG)) continue;
    tweet.setAttribute(HOVER_FLAG, '1');
    tweet.addEventListener('mouseenter', () => placePillIn(tweet));
  }
}

function placePillIn(tweet) {
  if (tweet.querySelector(`af-pill[${PILL_FLAG}]`)) return;
  const link = tweet.querySelector('a[href*="/status/"]');
  if (!link) return;
  const url = twitterUrl(link.href);
  if (!url) return;
  const pill = document.createElement('af-pill');
  pill.setAttribute(PILL_FLAG, '1');
  pill.dataset.url = url;
  pill.dataset.source = 'twitter';
  pill.style.position = 'absolute';
  pill.style.top = '8px';
  pill.style.right = '8px';
  pill.style.zIndex = '999';
  tweet.style.position = tweet.style.position || 'relative';
  tweet.appendChild(pill);
}
