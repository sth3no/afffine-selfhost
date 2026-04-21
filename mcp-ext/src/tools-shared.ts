/**
 * Shared type for tool definitions. Split out so both the read-side tool
 * module (tools.ts) and the write-side (write-tools.ts) can import it
 * without circular deps.
 */

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

export interface ToolDefinition {
  name: string;
  description: string;
  inputSchema: ToolInputSchema;
  handler: (token: string, args: Record<string, unknown>) => Promise<string>;
}
