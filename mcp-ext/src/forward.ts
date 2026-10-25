/**
 * Forwarder to AFFiNE's built-in MCP endpoint.
 *
 * For tools that AFFiNE already provides natively (read_document,
 * doc_search), we don't reimplement them — we proxy the tools/call
 * JSON-RPC straight through and pass the response back to the client.
 *
 * Auth: AFFiNE 0.27 only accepts MCP credentials here
 * (`Authorization: Bearer aff_mcp_v1.…`). The credential comes from
 * {@link McpCredentialManager}; on HTTP 401 it is re-created and the
 * request retried once.
 */

import type { FetchLike } from './affine-auth.js';

interface JsonRpcResponse {
  jsonrpc: '2.0';
  id: number | string | null;
  result?: unknown;
  error?: { code: number; message: string };
}

/** What the forwarder needs from the credential manager. */
export interface NativeCredentialSource {
  token(): Promise<string>;
  rejected(rejectedToken: string): Promise<string>;
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

export class NativeMcpClient {
  /** Token the initialize handshake was done with (redo it after a change). */
  private initializedWith: string | null = null;
  private sessionId: string | null = null;
  private readonly fetchImpl: FetchLike;

  constructor(
    private readonly url: string,
    private readonly credentials: NativeCredentialSource,
    fetchImpl?: FetchLike,
  ) {
    this.fetchImpl = fetchImpl ?? ((input, init) => fetch(input, init));
  }

  private send(token: string, body: Record<string, unknown>): Promise<Response> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      Accept: 'application/json, text/event-stream',
      Authorization: `Bearer ${token}`,
    };
    if (this.sessionId) headers['Mcp-Session-Id'] = this.sessionId;
    return this.fetchImpl(this.url, { method: 'POST', headers, body: JSON.stringify(body) });
  }

  /** POST one JSON-RPC message; on 401 re-create the credential and retry once. */
  private async post(body: Record<string, unknown>): Promise<Response> {
    let token = await this.credentials.token();
    let res = await this.send(token, body);
    if (res.status === 401) {
      await res.text().catch(() => undefined);
      token = await this.credentials.rejected(token);
      this.sessionId = null;
      res = await this.send(token, body);
    }

    if (!res.ok) {
      const text = await res.text().catch(() => '');
      throw new Error(`Native MCP HTTP ${res.status} ${res.statusText}: ${text.slice(0, 200)}`);
    }

    const newSession = res.headers.get('mcp-session-id');
    if (newSession) this.sessionId = newSession;
    return res;
  }

  /** Send a request (expects a response body) and parse JSON-RPC reply. */
  private async rpc(body: Record<string, unknown>): Promise<JsonRpcResponse> {
    const res = await this.post(body);
    const ct = res.headers.get('content-type') ?? '';
    if (ct.includes('text/event-stream')) return parseSse(res);
    return (await res.json()) as JsonRpcResponse;
  }

  /** Send a JSON-RPC notification (server returns 202 with no body). */
  private async notify(body: Record<string, unknown>): Promise<void> {
    const res = await this.post(body);
    // Drain the (likely empty) body to free the socket. Don't parse —
    // notifications return 202 Accepted without a JSON body.
    await res.text().catch(() => undefined);
  }

  /** Ensure the MCP handshake was done with the current credential. */
  private async ensureInitialized(): Promise<void> {
    const token = await this.credentials.token();
    if (this.initializedWith === token) return;

    await this.rpc({
      jsonrpc: '2.0',
      id: 'init',
      method: 'initialize',
      params: {
        protocolVersion: '2025-03-26',
        capabilities: {},
        clientInfo: { name: 'affine-mcp-ext', version: '1.4.0' },
      },
    });
    await this.notify({ jsonrpc: '2.0', method: 'notifications/initialized' });
    this.initializedWith = await this.credentials.token();
  }

  /** List native AFFiNE tools. */
  async listTools(): Promise<Array<{ name: string; description: string; inputSchema: unknown }>> {
    await this.ensureInitialized();
    const response = await this.rpc({ jsonrpc: '2.0', id: 'list', method: 'tools/list' });
    if (response.error) throw new Error(response.error.message);
    const r = response.result as {
      tools: Array<{ name: string; description: string; inputSchema: unknown }>;
    };
    return r.tools ?? [];
  }

  /** Forward a tools/call to AFFiNE native MCP and return its result. */
  async callTool(name: string, args: Record<string, unknown>): Promise<unknown> {
    await this.ensureInitialized();
    const response = await this.rpc({
      jsonrpc: '2.0',
      id: Date.now(),
      method: 'tools/call',
      params: { name, arguments: args },
    });
    if (response.error) throw new Error(response.error.message);
    return response.result;
  }
}
