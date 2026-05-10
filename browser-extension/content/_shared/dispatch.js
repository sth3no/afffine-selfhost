/**
 * Single content-script-side helper to send a capture request to the
 * background service worker. Used by the shared <af-pill> component.
 */
export async function dispatchCapture(payload) {
  return await chrome.runtime.sendMessage({ type: 'capture', payload });
}
