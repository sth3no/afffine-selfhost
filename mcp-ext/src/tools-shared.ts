/**
 * Shared type for tool definitions. Split out so both the read-side tool
 * module (tools.ts) and the write-side (write-tools.ts) can import it
 * without circular deps.
 */

import type { AffineSession } from './affine-auth.js';
import type { NativeMcpClient } from './forward.js';

/**
 * Minimal JSON Schema subset that AFFiNE / MCP clients understand for
 * tools/list input schemas. Relaxed to an open-ended record so tools can
 * declare nested `items` on array fields (e.g. arrays of block specs).
 */
export interface ToolInputSchema {
  type: 'object';
  properties: Record<string, Record<string, unknown>>;
  required?: string[];
}

/**
 * How tool handlers reach AFFiNE. Both are server-side identities of
 * mcp-ext itself — the MCP client's own bearer token is never forwarded.
 */
export interface AffineContext {
  /** Cookie session for GraphQL, blob upload and the Socket.IO sync gateway. */
  session: AffineSession;
  /** AFFiNE's built-in workspace MCP endpoint (MCP-credential auth). */
  native: NativeMcpClient;
}

export interface ToolDefinition {
  name: string;
  description: string;
  inputSchema: ToolInputSchema;
  handler: (ctx: AffineContext, args: Record<string, unknown>) => Promise<string>;
}
