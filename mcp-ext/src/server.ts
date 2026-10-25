/**
 * MCP Extension Proxy — HTTP server
 *
 * Implements two MCP HTTP transports so both new and legacy clients work:
 *
 *   1. Streamable HTTP (spec 2025-03-26) — single POST /, response is either
 *      `application/json` or `text/event-stream` per client's Accept header.
 *      This is what the built-in n8n / scheduler / Claude Desktop (new)
 *      clients talk.
 *
 *   2. Legacy SSE (spec 2024-11-05) — GET / with Accept: text/event-stream
 *      opens a persistent stream; server pushes an `endpoint` event with
 *      a per-session POST URL (`/messages?sessionId=<id>`). Client then
 *      POSTs JSON-RPC requests to that URL; the server writes responses
 *      back into the open SSE stream. This is what Claude Code 0.2.x and
 *      older Cursor builds speak when configured with `type: "sse"`.
 *
 * We deliberately don't use @modelcontextprotocol/sdk — the spec is small
 * and we already understand it from writing the agent's client.
 *
 * Auth (AFFiNE 0.27+):
 *   - Clients must send `Authorization: Bearer <MCP_EXT_HTTP_TOKEN>` on every
 *     MCP request (POST /, GET / SSE, POST /messages). That token is only
 *     checked here and is never forwarded.
 *   - Towards AFFiNE the proxy uses its own server-side identity: an
 *     email/password cookie session for GraphQL/blob/Socket.IO and an MCP
 *     credential for the native MCP endpoint (affine-auth.ts).
 */

import http, { type IncomingMessage, type ServerResponse } from 'node:http';
import { randomUUID } from 'node:crypto';
import { config, nativeMcpUrl } from './config.js';
import { tools, toolByName } from './tools.js';
import { AffineSession, McpCredentialManager } from './affine-auth.js';
import { createClientGate } from './client-auth.js';
import { NativeMcpClient } from './forward.js';
import type { AffineContext } from './tools-shared.js';

const SERVER_INFO = { name: 'affine-mcp-ext', version: '1.4.0' };

// ── AFFiNE identity (shared by all clients) ─────────────────────────

const session = new AffineSession({
  baseUrl: config.affineBaseUrl,
  email: config.affineEmail,
  password: config.affinePassword,
  clientVersion: config.affineClientVersion,
});
const credentials = new McpCredentialManager(session, {
  workspaceId: config.workspaceId,
  name: config.mcpCredentialName,
});
const ctx: AffineContext = {
  session,
  native: new NativeMcpClient(nativeMcpUrl(), credentials),
};

const isAuthorizedClient = createClientGate(config.httpToken);
const PROTOCOL_VERSION = '2025-03-26';

interface JsonRpcRequest {
  jsonrpc: '2.0';
  id?: number | string | null;
  method: string;
  params?: Record<string, unknown>;
}

type JsonRpcResult = {
  jsonrpc: '2.0';
  id: number | string | null;
  result?: unknown;
  error?: { code: number; message: string };
};

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', chunk => (data += chunk));
    req.on('end', () => resolve(data));
    req.on('error', reject);
  });
}

function sendUnauthorized(res: ServerResponse) {
  send(
    res,
    401,
    {
      jsonrpc: '2.0',
      id: null,
      error: { code: -32001, message: 'Unauthorized: send Authorization: Bearer <MCP_EXT_HTTP_TOKEN>.' },
    },
    { 'WWW-Authenticate': 'Bearer realm="affine-mcp-ext"' },
  );
}

function send(res: ServerResponse, status: number, body: unknown, extraHeaders: Record<string, string> = {}) {
  const payload = typeof body === 'string' ? body : JSON.stringify(body);
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(payload).toString(),
    ...extraHeaders,
  });
  res.end(payload);
}

/**
 * Streamable HTTP inline SSE response: a single `message` event, then end.
 * Used when a POST client sends `Accept: text/event-stream` *only* (no
 * `application/json`). Distinct from the persistent-stream SSE transport
 * below (openSseSession), which keeps the connection open across requests.
 */
function sendInlineSse(res: ServerResponse, body: unknown) {
  const json = typeof body === 'string' ? body : JSON.stringify(body);
  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache, no-transform',
    Connection: 'keep-alive',
    'X-Accel-Buffering': 'no',
  });
  res.write(`event: message\ndata: ${json}\n\n`);
  res.end();
}

function wantsInlineSseResponse(req: IncomingMessage): boolean {
  const accept = req.headers['accept'];
  if (!accept) return false;
  const v = Array.isArray(accept) ? accept.join(',') : accept;
  const wantsSse = /text\/event-stream/i.test(v);
  const wantsJson = /application\/json/i.test(v);
  return wantsSse && !wantsJson;
}

// ── Legacy SSE (2024-11-05) transport ───────────────────────────────

interface SseSession {
  res: ServerResponse;
  heartbeat: NodeJS.Timeout;
}
const sseSessions = new Map<string, SseSession>();

/**
 * Open a persistent SSE stream. Emits the `endpoint` event with a
 * per-session POST URL, then keeps the connection alive with periodic
 * heartbeats until the client disconnects.
 */
function openSseSession(req: IncomingMessage, res: ServerResponse): void {
  const sessionId = randomUUID();

  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache, no-transform',
    Connection: 'keep-alive',
    'X-Accel-Buffering': 'no',
  });

  // First event per MCP spec — tells client where to POST requests.
  // Path is relative to the server origin the client opened the stream on.
  res.write(`event: endpoint\ndata: /messages?sessionId=${sessionId}\n\n`);

  const heartbeat = setInterval(() => {
    // Comment line serves as heartbeat per SSE spec; keeps proxies honest.
    try {
      res.write(`: heartbeat\n\n`);
    } catch {
      // Socket closed mid-write; cleanup happens in close handler below.
    }
  }, 15000);
  heartbeat.unref?.();

  sseSessions.set(sessionId, { res, heartbeat });

  const cleanup = () => {
    clearInterval(heartbeat);
    sseSessions.delete(sessionId);
  };
  req.on('close', cleanup);
  res.on('close', cleanup);
}

/**
 * Route a POSTed JSON-RPC request (from a legacy-SSE client) to the open
 * SSE stream for that session. Replies with 202 on the POST itself; the
 * actual response payload travels back over the SSE stream.
 */
async function handleSseMessage(
  req: IncomingMessage,
  res: ServerResponse,
  sessionId: string
): Promise<void> {
  const session = sseSessions.get(sessionId);
  if (!session) {
    return send(res, 404, {
      jsonrpc: '2.0',
      id: null,
      error: {
        code: -32000,
        message: `Unknown SSE session "${sessionId}". Open GET / with Accept: text/event-stream first.`,
      },
    });
  }

  let body: string;
  try {
    body = await readBody(req);
  } catch (err) {
    return send(res, 400, { error: `Failed to read body: ${(err as Error).message}` });
  }

  let parsed: JsonRpcRequest;
  try {
    parsed = JSON.parse(body);
  } catch {
    return send(res, 400, { jsonrpc: '2.0', id: null, error: { code: -32700, message: 'Parse error' } });
  }

  // Per spec the POST returns 202 with no body — the real response goes
  // back on the SSE stream for the session.
  res.writeHead(202);
  res.end();

  const response = await dispatch(parsed);
  if (response === null) return; // notification

  try {
    session.res.write(`event: message\ndata: ${JSON.stringify(response)}\n\n`);
  } catch {
    // Stream broken; drop session.
    clearInterval(session.heartbeat);
    sseSessions.delete(sessionId);
  }
}

// ── Dispatcher ─────────────────────────────────────────────────────

async function dispatch(req: JsonRpcRequest): Promise<JsonRpcResult | null> {
  const id = req.id ?? null;

  // Notifications have no id and expect no response
  if (req.id === undefined) {
    switch (req.method) {
      case 'notifications/initialized':
      case 'notifications/cancelled':
        return null;
      default:
        return null; // silently ignore unknown notifications
    }
  }

  try {
    switch (req.method) {
      case 'initialize': {
        return {
          jsonrpc: '2.0',
          id,
          result: {
            protocolVersion: PROTOCOL_VERSION,
            capabilities: { tools: {} },
            serverInfo: SERVER_INFO,
          },
        };
      }

      case 'tools/list': {
        return {
          jsonrpc: '2.0',
          id,
          result: {
            tools: tools.map(t => ({
              name: t.name,
              description: t.description,
              inputSchema: t.inputSchema,
            })),
          },
        };
      }

      case 'tools/call': {
        const { name, arguments: args } = (req.params ?? {}) as {
          name?: string;
          arguments?: Record<string, unknown>;
        };
        if (!name) {
          return { jsonrpc: '2.0', id, error: { code: -32602, message: 'Missing tool name' } };
        }
        const tool = toolByName.get(name);
        if (!tool) {
          // Per MCP spec, tool errors are reported via isError on a successful
          // result, not via JSON-RPC error — so callers handle them uniformly.
          return {
            jsonrpc: '2.0',
            id,
            result: {
              content: [{ type: 'text', text: `Tool "${name}" is not exposed by this proxy. Call tools/list to see what is available.` }],
              isError: true,
            },
          };
        }

        try {
          const text = await tool.handler(ctx, args ?? {});
          return {
            jsonrpc: '2.0',
            id,
            result: {
              content: [{ type: 'text', text }],
            },
          };
        } catch (err) {
          return {
            jsonrpc: '2.0',
            id,
            result: {
              content: [
                { type: 'text', text: `Tool "${name}" failed: ${(err as Error).message}` },
              ],
              isError: true,
            },
          };
        }
      }

      case 'ping':
        return { jsonrpc: '2.0', id, result: {} };

      default:
        return {
          jsonrpc: '2.0',
          id,
          error: { code: -32601, message: `Method not found: ${req.method}` },
        };
    }
  } catch (err) {
    return {
      jsonrpc: '2.0',
      id,
      error: { code: -32603, message: `Internal error: ${(err as Error).message}` },
    };
  }
}

// ── HTTP router ────────────────────────────────────────────────────

const server = http.createServer(async (req, res) => {
  const url = req.url ?? '/';
  const method = req.method ?? 'GET';
  const accept = Array.isArray(req.headers.accept) ? req.headers.accept.join(',') : (req.headers.accept ?? '');

  // Health endpoint (explicit path — doesn't conflict with legacy SSE GET /)
  if (method === 'GET' && url === '/health') {
    return send(res, 200, { status: 'ok', server: SERVER_INFO });
  }

  const isSseOpen =
    method === 'GET' && /text\/event-stream/i.test(accept) && (url === '/' || url.startsWith('/sse'));
  const isSseMessage = method === 'POST' && url.startsWith('/messages');
  const isStreamableHttp = method === 'POST' && url === '/';

  // Every MCP entry point requires the client token (health stays open).
  if ((isSseOpen || isSseMessage || isStreamableHttp) && !isAuthorizedClient(req.headers['authorization'])) {
    return sendUnauthorized(res);
  }

  // Legacy SSE transport — open persistent stream on GET (or GET /sse)
  if (isSseOpen) {
    return openSseSession(req, res);
  }

  // Legacy SSE transport — per-session POST for JSON-RPC requests
  if (isSseMessage) {
    const u = new URL(url, 'http://localhost');
    const sessionId = u.searchParams.get('sessionId') ?? '';
    return handleSseMessage(req, res, sessionId);
  }

  // GET / without SSE Accept → backward-compatible health JSON
  if (method === 'GET' && url === '/') {
    return send(res, 200, { status: 'ok', server: SERVER_INFO });
  }

  // Streamable HTTP transport — POST / with JSON-RPC body
  if (!isStreamableHttp) {
    return send(res, 405, { error: `Method Not Allowed: ${method} ${url}. Expected POST / (Streamable HTTP), GET / (legacy SSE), or POST /messages?sessionId=... (legacy SSE request).` });
  }

  let body: string;
  try {
    body = await readBody(req);
  } catch (err) {
    return send(res, 400, { error: `Failed to read body: ${(err as Error).message}` });
  }

  let parsed: JsonRpcRequest;
  try {
    parsed = JSON.parse(body);
  } catch {
    return send(res, 400, {
      jsonrpc: '2.0',
      id: null,
      error: { code: -32700, message: 'Parse error' },
    });
  }

  const response = await dispatch(parsed);

  if (response === null) {
    // Notification — acknowledge with 202 No Content
    res.writeHead(202);
    res.end();
    return;
  }

  if (wantsInlineSseResponse(req)) {
    sendInlineSse(res, response);
  } else {
    send(res, 200, response);
  }
});

server.listen(config.port, () => {
  console.log(`[mcp-ext] Listening on :${config.port}`);
  console.log(`[mcp-ext] Proxying AFFiNE at ${config.affineBaseUrl}`);
  console.log(`[mcp-ext] Workspace: ${config.workspaceId}`);
  console.log(`[mcp-ext] Client auth: Bearer MCP_EXT_HTTP_TOKEN; AFFiNE auth: own session (AFFINE_MCP_EMAIL) + managed MCP credential "${credentials.name}"`);
  console.log(`[mcp-ext] Transports: Streamable HTTP (POST /), legacy SSE (GET /, POST /messages?sessionId=...)`);
  console.log(`[mcp-ext] Exposing ${tools.length} tools:`);
  for (const t of tools) console.log(`  - ${t.name}`);
  // Warm up the AFFiNE session so a wrong password / missing account shows
  // up in the logs at boot instead of on the first tool call.
  session.signIn().catch(err => {
    console.error(`[mcp-ext] AFFiNE sign-in failed at startup (will retry on demand): ${(err as Error).message}`);
  });
});

// Graceful shutdown
for (const sig of ['SIGTERM', 'SIGINT'] as const) {
  process.on(sig, () => {
    console.log(`[mcp-ext] ${sig} received, closing…`);
    // End all open SSE sessions so clients disconnect cleanly
    for (const [id, s] of sseSessions) {
      try { s.res.end(); } catch { /* ignore */ }
      clearInterval(s.heartbeat);
      sseSessions.delete(id);
    }
    server.close(() => process.exit(0));
  });
}
