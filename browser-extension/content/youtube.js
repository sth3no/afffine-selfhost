/**
 * YouTube content script. Injects an <af-pill> into the action row
 * (Save / Share buttons) of the watch page. Uses MutationObserver to
 * survive YouTube's SPA navigation.
 */
import { youtubeUrl } from './_shared/canonicalizers.js';
import './_shared/pill.js';

const ANCHOR_SELECTOR = 'ytd-watch-metadata #actions';
const PILL_FLAG = 'data-af-pill';

let observer = null;
let attemptedAt = 0;

attach();

function attach() {
  if (placePill()) return;
  if (observer) observer.disconnect();
  attemptedAt = Date.now();
  observer = new MutationObserver(() => {
    if (placePill()) return;
    if (Date.now() - attemptedAt > 10000) {
      console.warn('[AFFiNE Capture] anchor not found on youtube');
      observer.disconnect();
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });
}

function placePill() {
  const anchor = document.querySelector(ANCHOR_SELECTOR);
  if (!anchor) return false;
  if (anchor.querySelector(`af-pill[${PILL_FLAG}]`)) return true;
  const url = youtubeUrl(window.location.href);
  if (!url) return false;
  const pill = document.createElement('af-pill');
  pill.setAttribute(PILL_FLAG, '1');
  pill.dataset.url = url;
  pill.dataset.source = 'youtube';
  pill.dataset.title = document.title.replace(/ - YouTube$/, '');
  pill.style.marginLeft = '8px';
  anchor.appendChild(pill);
  return true;
}

const origPush = history.pushState;
history.pushState = function (...args) {
  origPush.apply(this, args);
  setTimeout(attach, 200);
};
window.addEventListener('popstate', () => setTimeout(attach, 200));
