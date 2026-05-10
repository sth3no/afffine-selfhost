/** @vitest-environment node */
import { describe, it, expect } from 'vitest';
import {
  linkIcon, checkIcon, xCircleIcon, arrowUpRightIcon,
  arrowClockwiseIcon, trashIcon, playRectangleIcon,
  cameraIcon, xLogoIcon, musicNoteIcon, redditIcon,
  platformIcon,
} from '../icons.js';

describe('lib/icons', () => {
  const all = {
    linkIcon, checkIcon, xCircleIcon, arrowUpRightIcon,
    arrowClockwiseIcon, trashIcon, playRectangleIcon,
    cameraIcon, xLogoIcon, musicNoteIcon, redditIcon,
  };

  it('all exports are non-empty SVG strings starting with <svg', () => {
    for (const [name, svg] of Object.entries(all)) {
      expect(svg, name).toMatch(/^<svg[\s>]/);
      expect(svg, name).toMatch(/<\/svg>$/);
    }
  });

  it('uses currentColor for stroke (no hardcoded color)', () => {
    for (const [name, svg] of Object.entries(all)) {
      expect(svg, `${name} should use currentColor`).toMatch(/stroke="currentColor"|fill="currentColor"/);
    }
  });

  it('uses 2px stroke width', () => {
    for (const [name, svg] of Object.entries(all)) {
      if (svg.includes('stroke="none"') || !svg.includes('stroke=')) continue;
      expect(svg, `${name} stroke-width`).toMatch(/stroke-width="2"/);
    }
  });

  it('platformIcon maps known platforms', () => {
    expect(platformIcon('youtube')).toBe(playRectangleIcon);
    expect(platformIcon('instagram')).toBe(cameraIcon);
    expect(platformIcon('x')).toBe(xLogoIcon);
    expect(platformIcon('twitter')).toBe(xLogoIcon);
    expect(platformIcon('tiktok')).toBe(musicNoteIcon);
    expect(platformIcon('reddit')).toBe(redditIcon);
  });

  it('platformIcon falls back to linkIcon for unknown', () => {
    expect(platformIcon('article')).toBe(linkIcon);
    expect(platformIcon('unknown-platform')).toBe(linkIcon);
    expect(platformIcon(null)).toBe(linkIcon);
  });
});
