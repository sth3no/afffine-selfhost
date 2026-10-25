/**
 * Client → mcp-ext gate.
 *
 * Every MCP request must carry `Authorization: Bearer <MCP_EXT_HTTP_TOKEN>`.
 * The token only unlocks this proxy — it is never forwarded to AFFiNE
 * (mcp-ext talks to AFFiNE with its own server-side session, see
 * affine-auth.ts).
 */

import { createHash, timingSafeEqual } from 'node:crypto';

function digest(value: string): Buffer {
  return createHash('sha256').update(value, 'utf8').digest();
}

/** Extract the token from an `Authorization: Bearer <token>` header value. */
export function parseBearer(header: string | string[] | undefined): string | null {
  const value = Array.isArray(header) ? header[0] : header;
  if (!value) return null;
  const m = /^Bearer\s+(\S+)\s*$/i.exec(value);
  return m ? m[1] : null;
}

/**
 * Build a checker for the expected shared secret. Comparison is constant
 * time: both sides are hashed first, so neither content nor length of the
 * expected token leaks through timing.
 */
export function createClientGate(expectedToken: string): (header: string | string[] | undefined) => boolean {
  if (!expectedToken) {
    throw new Error('MCP_EXT_HTTP_TOKEN must be set');
  }
  const expected = digest(expectedToken);
  return header => {
    const presented = parseBearer(header);
    if (!presented) return false;
    return timingSafeEqual(digest(presented), expected);
  };
}
