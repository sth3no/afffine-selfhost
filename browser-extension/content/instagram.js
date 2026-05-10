/**
 * Instagram content script. Two injection strategies:
 *
 *  1. Feed grid (`article[role=presentation]`) — hover-triggered pill inside
 *     each article (existing behavior).
 *  2. Reels viewer — locate the right-side action column via stable
 *     aria-label selectors on inner SVGs (Like/Save/Comment), then insert
 *     the pill between Share and Save buttons in the column.
 *
 * URL updates on SPA navigation: each reel scroll triggers history.pushState
 * which updates window.location.href; we patch pushState + listen to popstate
 * to keep the pill's data-url current.
 */
(async () => {
  const canonicalizers = await import(chrome.runtime.getURL('content/_shared/canonicalizers.js'));
  await import(chrome.runtime.getURL('content/_shared/pill.js'));
  const { instagramUrl } = canonicalizers;

  const PILL_FLAG = 'data-af-pill';
  const HOVER_FLAG = 'data-af-hover';
  const FEED_POST_SELECTOR = 'article[role=presentation]';

  // ── Feed posts: hover-triggered pill ─────────────────────────
  function placePillInFeedPost(post) {
    if (post.querySelector(`af-pill[${PILL_FLAG}]`)) return;
    const link = post.querySelector('a[href*="/p/"], a[href*="/reel/"], a[href*="/reels/"]');
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

  // ── Reels viewer: action-column pill ─────────────────────────

  /**
   * Find every reels-viewer action column on the page. A column is the
   * common ancestor of a Like SVG and a Save SVG (with Comment also nearby
   * to avoid false positives in the feed grid).
   */
  function findReelActionColumns() {
    const columns = new Set();
    for (const likeSvg of document.querySelectorAll('svg[aria-label="Like"]')) {
      let node = likeSvg.parentElement;
      let hops = 0;
      while (node && node !== document.body && hops++ < 12) {
        if (node.querySelector('svg[aria-label="Save"]')
            && node.querySelector('svg[aria-label="Comment"]')
            && node.querySelector('svg[aria-label="Share"]')) {
          columns.add(node);
          break;
        }
        node = node.parentElement;
      }
    }
    return [...columns];
  }

  /**
   * Find the wrapper element that contains a single action button (the
   * <div> that wraps Share's SVG with its surrounding chrome). This is
   * 4–6 ancestors above the SVG; we walk up looking for the highest
   * ancestor that's still scoped to ONE button (i.e. its parent contains
   * sibling action buttons).
   */
  function findActionWrapper(svg, column) {
    let node = svg.parentElement;
    while (node && node.parentElement && node !== column) {
      // Stop when the parent is the column itself or a direct row container.
      if (node.parentElement === column
          || node.parentElement.querySelectorAll('svg[aria-label]').length > 1) {
        return node;
      }
      node = node.parentElement;
    }
    return svg.closest('[role="button"]')?.parentElement ?? svg.parentElement;
  }

  function placePillInReelColumn(column) {
    if (column.querySelector(`af-pill[${PILL_FLAG}]`)) return;
    const pill = document.createElement('af-pill');
    pill.setAttribute(PILL_FLAG, '1');
    pill.dataset.source = 'instagram';
    pill.dataset.url = currentReelUrl();

    const shareSvg = column.querySelector('svg[aria-label="Share"]');
    const shareWrapper = shareSvg ? findActionWrapper(shareSvg, column) : null;

    // Wrap the pill in a div that mimics the column's per-button spacing.
    const pillWrapper = document.createElement('div');
    pillWrapper.style.cssText = 'display: flex; justify-content: center; padding: 8px 0;';
    pillWrapper.appendChild(pill);

    if (shareWrapper && shareWrapper.parentElement) {
      shareWrapper.parentElement.insertBefore(pillWrapper, shareWrapper.nextSibling);
    } else {
      column.appendChild(pillWrapper);
    }
  }

  function currentReelUrl() {
    const canonical = instagramUrl(window.location.href);
    if (canonical) return canonical;
    // Fallback: look for a /reel/ link in the page.
    const link = document.querySelector('a[href*="/reel/"], a[href*="/reels/"]');
    if (link) {
      const fromLink = instagramUrl(link.href);
      if (fromLink) return fromLink;
    }
    return window.location.href;
  }

  function refreshReelPillUrls() {
    const url = currentReelUrl();
    for (const pill of document.querySelectorAll(`af-pill[${PILL_FLAG}][data-source="instagram"]`)) {
      pill.dataset.url = url;
    }
  }

  // ── Scan loop ────────────────────────────────────────────────
  function scan() {
    for (const post of document.querySelectorAll(FEED_POST_SELECTOR)) {
      if (post.hasAttribute(HOVER_FLAG)) continue;
      post.setAttribute(HOVER_FLAG, '1');
      post.addEventListener('mouseenter', () => placePillInFeedPost(post));
    }
    for (const column of findReelActionColumns()) {
      placePillInReelColumn(column);
    }
  }

  const observer = new MutationObserver(() => {
    scan();
    refreshReelPillUrls();
  });
  observer.observe(document.body, { childList: true, subtree: true });
  scan();

  // ── SPA navigation: patch pushState + listen to popstate ─────
  const origPush = history.pushState;
  history.pushState = function (...args) {
    origPush.apply(this, args);
    setTimeout(refreshReelPillUrls, 100);
  };
  window.addEventListener('popstate', () => setTimeout(refreshReelPillUrls, 100));
})();
