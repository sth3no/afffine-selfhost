/**
 * Context-menu integration. On install, registers four menu items
 * (page / link / selection / image). On click, builds a CaptureRequest
 * payload and dispatches via performCapture(), then shows a notification.
 *
 * Clicking the notification opens the AFFiNE doc URL in a new tab.
 */
import { buildPayloadFromTab } from './payload.js';
import { performCapture } from './handler.js';

const MENU_IDS = {
  page: 'affine-capture-page',
  link: 'affine-capture-link',
  selection: 'affine-capture-selection',
  image: 'affine-capture-image',
};

/**
 * Map from notification ID to web_url. Lets onClicked open the right doc.
 * Service-worker memory only; cleared when the worker dies — that's OK,
 * notifications are short-lived UI.
 */
const notificationOpenUrls = new Map();

/**
 * Register the four context menu items. Idempotent — safe to call on
 * every onInstalled (chrome.contextMenus removes existing items with the
 * same ID via removeAll first).
 */
export function registerContextMenus() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU_IDS.page,
      title: 'Save page to AFFiNE',
      contexts: ['page'],
    });
    chrome.contextMenus.create({
      id: MENU_IDS.link,
      title: 'Save link to AFFiNE',
      contexts: ['link'],
    });
    chrome.contextMenus.create({
      id: MENU_IDS.selection,
      title: 'Save selection to AFFiNE',
      contexts: ['selection'],
    });
    chrome.contextMenus.create({
      id: MENU_IDS.image,
      title: 'Save image to AFFiNE',
      contexts: ['image'],
    });
  });
}

/**
 * Handle a context menu click: build payload, perform capture, notify.
 */
export async function handleContextMenuClick(info, tab) {
  if (!tab) return;
  if (!Object.values(MENU_IDS).includes(info.menuItemId)) return;

  const payload = buildPayloadFromTab(tab, info);
  const result = await performCapture(payload);

  if (result.ok) {
    showSuccessNotification(result);
  } else {
    showErrorNotification(result.error);
  }
}

/**
 * Wire the notification-clicked listener so a click on the success toast
 * opens the AFFiNE doc URL in a new tab.
 */
export function registerNotificationHandlers() {
  chrome.notifications.onClicked.addListener(notificationId => {
    const url = notificationOpenUrls.get(notificationId);
    if (url) {
      chrome.tabs.create({ url });
      chrome.notifications.clear(notificationId);
      notificationOpenUrls.delete(notificationId);
    }
  });
  chrome.notifications.onClosed.addListener(notificationId => {
    notificationOpenUrls.delete(notificationId);
  });
}

function showSuccessNotification(response) {
  const id = `affine-capture-${response.capture_id}`;
  notificationOpenUrls.set(id, response.web_url);
  chrome.notifications.create(id, {
    type: 'basic',
    iconUrl: chrome.runtime.getURL('icons/icon-128.png'),
    title: 'Saved to AFFiNE',
    message: `${response.platform} · ${response.initial_path}\nClick to open`,
    priority: 0,
  });
}

function showErrorNotification(err) {
  const title = err.kind === 'invalid_token'
    ? 'Token rejected'
    : err.kind === 'config'
    ? 'AFFiNE Capture not configured'
    : err.kind === 'rate_limited'
    ? 'Rate limited'
    : err.kind === 'network'
    ? 'Couldn\'t reach ingest'
    : 'Capture failed';
  chrome.notifications.create({
    type: 'basic',
    iconUrl: chrome.runtime.getURL('icons/icon-128.png'),
    title,
    message: err.message ?? 'See AFFiNE Capture for details',
    priority: 1,
  });
}
