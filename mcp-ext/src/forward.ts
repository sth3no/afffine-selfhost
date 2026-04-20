/**
 * Forwarder to AFFiNE's built-in MCP endpoint.
 *
 * For tools that AFFiNE already provides natively (doc-read,
 * doc-keyword-search, doc-semantic-search), we don't reimplement them —
 * we proxy the tools/call JSON-RPC straight through and pass the response
 * back to the client unchanged.
 *
 * This keeps behaviour identical for those tools even as AFFiNE evolves.
 */

import { nativeMcpUrl } from './config.js';

interface JsonRpcResponse {
  jsonrpc: '2.0';
  id: number | string | null;
  result?: unknown;
  error?: { code: number; message: string };
}

/** Per-token state so we only handshake with AFFiNE once per client. */
interface NativeSession {
  initialized: boolean;
  sessionId: string | null;
}
const sessions = new Map<string, NativeSession>();

function session(token: string): NativeSession {
  let s = sessions.get(token);
  if (!s) {
    s = { initialized: false, sessionId: null };
    sessions.set(token, s);
  }
  return s;
}

async function parseSse(res: Response): Promise<JsonRpcResponse> {
  const text = await res.text();
  const dataLines = text
    .split(/\r?\n/)
    .filter(l => l.startsWith('data:'))
    .map(l => l.slice(5).trimStart());
  if (dataLines.length === 0) {
    throw new Error(`Native MCP SSE had no data frame: ${text.slice(0, 200)}`);
  }
  return JSON.parse(dataLines.join('\n')) as JsonRpcResponse;
}

async function rpcToNative(
  token: string,
  body: Record<string, unknown>
): Promise<{ response: JsonRpcResponse; sessionId: string | null }> {
  const s = session(token);
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Accept: 'application/json, text/event-stream',
    Authorization: `Bearer ${token}`,
  };
  if (s.sessionId) headers['Mcp-Session-Id'] = s.sessionId;

  const res = await fetch(nativeMcpUrl(), {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`Native MCP HTTP ${res.status} ${res.statusText}: ${text.slice(0, 200)}`);
  }

  const newSession = res.headers.get('mcp-session-id');
  if (newSession) s.sessionId = newSession;

  const ct = res.headers.get('content-type') ?? '';
  const response = ct.includes('text/event-stream')
    ? await parseSse(res)
    : ((await res.json()) as JsonRpcResponse);

  return { response, sessionId: s.sessionId };
}

/** Ensure native MCP session is initialized before forwarding tool calls. */
async function ensureInitialized(token: string): Promise<void> {
  const s = session(token);
  if (s.initialized) return;

  await rpcToNative(token, {
    jsonrpc: '2.0',
    id: 'init',
    method: 'initialize',
    params: {
      protocolVersion: '2025-03-26',
      capabilities: {},
      clientInfo: { name: 'affine-mcp-ext', version: '1.0.0' },
    },
  });
  await rpcToNative(token, {
    jsonrpc: '2.0',
    method: 'notifications/initialized',
  });

  s.initialized = true;
}

/** List native AFFiNE tools so we can merge them into our tools/list response. */
export async function listNativeTools(token: string): Promise<
  Array<{ name: string; description: string; inputSchema: unknown }>
> {
  await ensureInitialized(token);
  const { response } = await rpcToNative(token, {
    jsonrpc: '2.0',
    id: 'list',
    method: 'tools/list',
  });
  if (response.error) throw new Error(response.error.message);
  const r = response.result as {
    tools: Array<{ name: string; description: string; inputSchema: unknown }>;
  };
  return r.tools ?? [];
}

/** Forward a tools/call to AFFiNE native MCP and return its result. */
export async function callNativeTool(
  token: string,
  name: string,
  args: Record<string, unknown>
): Promise<unknown> {
  await ensureInitialized(token);
  const { response } = await rpcToNative(token, {
    jsonrpc: '2.0',
    id: Date.now(),
    method: 'tools/call',
    params: { name, arguments: args },
  });
  if (response.error) throw new Error(response.error.message);
  return response.result;
}

/** Invalidate cached session for a token (e.g. on error). */
export function forgetSession(token: string): void {
  sessions.delete(token);
}
