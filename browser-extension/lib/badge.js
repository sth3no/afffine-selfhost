/**
 * Single source of truth for the toolbar badge.
 *
 * The badge can show a "!" warning from EITHER subsystem:
 *   - cookies-stale  (server cookies file old / missing)
 *   - capture-failed (last capture errored, e.g. token rejected)
 *
 * v0.2 collapses both into a single "!" — the popup explains which when
 * opened. State is persisted to chrome.storage.local so it survives the
 * service worker dying and restarting.
 */

const COLOR_WARN = '#d33a2c';   // matches v0.1 BADGE_COLOR_STALE
const TEXT_WARN = '!';

/**
 * Update badge state for one subsystem and recompute the visible badge.
 *
 * @param {'cookies' | 'capture'} subsystem
 * @param {'ok' | 'warn' | 'unknown'} state
 */
export async function setSubsystem(subsystem, state) {
  const { badgeState } = await chrome.storage.local.get('badgeState');
  const next = { ...(badgeState ?? {}), [subsystem]: state };
  await chrome.storage.local.set({ badgeState: next });
  await applyBadge(next);
}

/**
 * Re-apply the badge from current state (used at service-worker startup).
 */
export async function refreshBadge() {
  const { badgeState } = await chrome.storage.local.get('badgeState');
  await applyBadge(badgeState ?? {});
}

async function applyBadge(state) {
  const anyWarn = Object.values(state).some(s => s === 'warn');
  await chrome.action.setBadgeText({ text: anyWarn ? TEXT_WARN : '' });
  if (anyWarn) {
    await chrome.action.setBadgeBackgroundColor({ color: COLOR_WARN });
  }
}
