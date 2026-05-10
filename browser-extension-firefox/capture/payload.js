/**
 * Build a CaptureRequest-shaped payload from a chrome.tabs.Tab and optional
 * contextMenus.OnClickData info. The shape matches the server's Pydantic
 * model: { url, source_app?, shared_title?, shared_text? }.
 *
 * Precedence for `url`:
 *   1. info.linkUrl   (right-click "Save link")
 *   2. info.srcUrl    (right-click "Save image")
 *   3. tab.url        (popup / context-menu page / context-menu selection)
 *
 * `shared_text` only set when info.selectionText is non-empty.
 * `source_app` derived from tab.url's hostname; null for non-http(s) URLs.
 */
export function buildPayloadFromTab(tab, info = {}) {
  const url = info.linkUrl ?? info.srcUrl ?? tab.url;
  const sourceApp = hostnameOrNull(tab.url);
  const payload = {
    url,
    source_app: sourceApp,
    shared_title: tab.title ?? undefined,
  };
  if (info.selectionText) {
    payload.shared_text = info.selectionText;
  }
  return payload;
}

function hostnameOrNull(urlStr) {
  try {
    const u = new URL(urlStr);
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
    return u.hostname || null;
  } catch {
    return null;
  }
}
