/**
 * TikTok content script. Per-FYP-card pill on hover.
 * Anchor: `div[data-e2e=recommend-list-item-container]` (FYP) or any element
 * with a /@user/video/<id> link nearby.
 */
(async () => {
  const canonicalizers = await import(chrome.runtime.getURL('content/_shared/canonicalizers.js'));
  await import(chrome.runtime.getURL('content/_shared/pill.js'));
  const { tiktokUrl } = canonicalizers;

  const CARD_SELECTORS = [
    'div[data-e2e=recommend-list-item-container]',
    'div[data-e2e=user-post-item]',
  ];
  const PILL_FLAG = 'data-af-pill';
  const HOVER_FLAG = 'data-af-hover';

  const observer = new MutationObserver(scan);
  observer.observe(document.body, { childList: true, subtree: true });
  scan();

  function scan() {
    for (const sel of CARD_SELECTORS) {
      for (const card of document.querySelectorAll(sel)) {
        if (card.hasAttribute(HOVER_FLAG)) continue;
        card.setAttribute(HOVER_FLAG, '1');
        card.addEventListener('mouseenter', () => placePillIn(card));
      }
    }
  }

  function placePillIn(card) {
    if (card.querySelector(`af-pill[${PILL_FLAG}]`)) return;
    const link = card.querySelector('a[href*="/video/"]');
    if (!link) return;
    const url = tiktokUrl(link.href);
    if (!url) return;
    const pill = document.createElement('af-pill');
    pill.setAttribute(PILL_FLAG, '1');
    pill.dataset.url = url;
    pill.dataset.source = 'tiktok';
    pill.style.position = 'absolute';
    pill.style.top = '8px';
    pill.style.right = '8px';
    pill.style.zIndex = '999';
    card.style.position = card.style.position || 'relative';
    card.appendChild(pill);
  }
})();
