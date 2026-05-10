/**
 * SVG string library. All icons:
 *   - 2px stroke
 *   - currentColor (so they inherit from the surrounding text color)
 *   - 24x24 viewBox normalized
 *   - line-cap round / line-join round
 *
 * Used by Web Components (Phase 4) and history rows (Phase 6).
 *
 * Design references the AFFiNE spec §5: linear thin-stroke icon style.
 */

const SVG_ATTRS = 'xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"';

export const linkIcon = `<svg ${SVG_ATTRS}><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>`;

export const checkIcon = `<svg ${SVG_ATTRS}><polyline points="20 6 9 17 4 12"/></svg>`;

export const xCircleIcon = `<svg ${SVG_ATTRS}><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>`;

export const arrowUpRightIcon = `<svg ${SVG_ATTRS}><line x1="7" y1="17" x2="17" y2="7"/><polyline points="7 7 17 7 17 17"/></svg>`;

export const arrowClockwiseIcon = `<svg ${SVG_ATTRS}><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>`;

export const trashIcon = `<svg ${SVG_ATTRS}><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>`;

export const playRectangleIcon = `<svg ${SVG_ATTRS}><rect x="2" y="4" width="20" height="16" rx="2" ry="2"/><polygon points="10 9 16 12 10 15"/></svg>`;

export const cameraIcon = `<svg ${SVG_ATTRS}><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>`;

export const xLogoIcon = `<svg ${SVG_ATTRS}><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;

export const musicNoteIcon = `<svg ${SVG_ATTRS}><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>`;

export const redditIcon = `<svg ${SVG_ATTRS}><circle cx="12" cy="12" r="10"/><path d="M8 14a4 4 0 0 0 8 0"/><circle cx="9" cy="11" r="1" fill="currentColor"/><circle cx="15" cy="11" r="1" fill="currentColor"/></svg>`;

const PLATFORM_MAP = {
  youtube: playRectangleIcon,
  instagram: cameraIcon,
  x: xLogoIcon,
  twitter: xLogoIcon,
  tiktok: musicNoteIcon,
  reddit: redditIcon,
};

export function platformIcon(platform) {
  return PLATFORM_MAP[platform] ?? linkIcon;
}
