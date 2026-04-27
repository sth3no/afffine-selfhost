/**
 * Fractional-indexing helpers for sibling ordering in the folders table.
 *
 * AFFiNE orders sibling nodes (under the same parentId) by a string `index`
 * column. New entries are placed by generating a key that sorts strictly
 * between existing neighbours — so insertion is O(1) and never requires
 * re-indexing other rows.
 *
 * AFFiNE's wrapper (packages/common/infra/src/utils/fractional-indexing.ts)
 * is `generateKeyBetween(a, b) + randomSuffix(32)`. The random suffix breaks
 * ties when concurrent clients insert at the same position. We mirror the
 * same shape so keys we generate sort correctly relative to AFFiNE-generated
 * keys.
 */

import { generateKeyBetween } from 'fractional-indexing';

// fractional-indexing's default alphabet is base62; lowercase + digits is a
// strict subset, which keeps the suffix safely orderable alongside the base.
const SUFFIX_ALPHABET = '0123456789abcdefghijklmnopqrstuvwxyz';
const SUFFIX_LEN = 8;

function randomSuffix(): string {
  let s = '';
  for (let i = 0; i < SUFFIX_LEN; i++) {
    s += SUFFIX_ALPHABET[Math.floor(Math.random() * SUFFIX_ALPHABET.length)];
  }
  return s;
}

/**
 * Generate a fractional index strictly between `prev` and `next`.
 * Pass `null` for either bound to extend the list at start or end.
 */
export function indexBetween(prev: string | null, next: string | null): string {
  return generateKeyBetween(prev, next) + randomSuffix();
}

/** Index that sorts after every existing sibling's index. */
export function indexAfterAll(siblingIndices: string[]): string {
  if (siblingIndices.length === 0) return indexBetween(null, null);
  const sorted = [...siblingIndices].sort();
  return indexBetween(sorted[sorted.length - 1] ?? null, null);
}

/** Index that sorts before every existing sibling's index. */
export function indexBeforeAll(siblingIndices: string[]): string {
  if (siblingIndices.length === 0) return indexBetween(null, null);
  const sorted = [...siblingIndices].sort();
  return indexBetween(null, sorted[0] ?? null);
}
