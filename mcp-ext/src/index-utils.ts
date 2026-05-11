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

// fractional-indexing's validateOrderKey rejects any key whose fractional
// part ends in the smallest digit ('0' in base62). If randomSuffix() rolls
// a '0' on the tail, the generated key still sorts fine — but the row
// becomes a poisoned bound: the next generateKeyBetween() call that uses
// it throws `invalid order key: <key>`, blocking move_document and other
// inserts under the same parent. Constrain the final character to a
// non-zero digit so every key we emit is a valid future bound.
const SUFFIX_TAIL_ALPHABET = SUFFIX_ALPHABET.slice(1);

function randomSuffix(): string {
  let s = '';
  for (let i = 0; i < SUFFIX_LEN - 1; i++) {
    s += SUFFIX_ALPHABET[Math.floor(Math.random() * SUFFIX_ALPHABET.length)];
  }
  s += SUFFIX_TAIL_ALPHABET[Math.floor(Math.random() * SUFFIX_TAIL_ALPHABET.length)];
  return s;
}

// Rows written by older versions of this MCP — or by another client — may
// already carry trailing-'0' indices. We can't rewrite stored values from
// here, but we can pick a NEARBY valid key to feed into generateKeyBetween
// so insertion succeeds while still bracketing the original row correctly.
//
// `prev` bound: we want output STRICTLY GREATER than the stored prev.
//   Appending '1' yields a longer string with the same prefix and a
//   non-zero tail, which sorts strictly after the original and passes
//   validateOrderKey.
function sanitizePrev(prev: string | null): string | null {
  if (prev === null || !prev.endsWith('0')) return prev;
  return prev + '1';
}

// `next` bound: we want output STRICTLY LESS than the stored next.
//   Stripping trailing zeros yields a prefix of next, which sorts strictly
//   before it. The stripped result has a non-zero tail. (An all-zero key
//   would strip to empty — fall through and let the library complain, that
//   case isn't reachable from a well-formed AFFiNE workspace.)
function sanitizeNext(next: string | null): string | null {
  if (next === null || !next.endsWith('0')) return next;
  const stripped = next.replace(/0+$/, '');
  return stripped.length > 0 ? stripped : next;
}

/**
 * Generate a fractional index strictly between `prev` and `next`.
 * Pass `null` for either bound to extend the list at start or end.
 */
export function indexBetween(prev: string | null, next: string | null): string {
  return generateKeyBetween(sanitizePrev(prev), sanitizeNext(next)) + randomSuffix();
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
