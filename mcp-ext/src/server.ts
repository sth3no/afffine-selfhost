/**
 * MCP Extension Proxy — HTTP server
 *
 * Implements the MCP Streamable HTTP transport (spec 2025-03-26) well
 * enough to serve:
 *   - initialize
 *   - notifications/initialized
 *   - tools/list
 *   - tools/call
 *
 * Listens on POST / — that's all a streamable-http transport needs.
 *
 * We deliberately don't use @modelcontextprotocol/sdk — the spec is
 * small and we already understand it from writing the agent's client.
 */

import http, { type IncomingMessage, type ServerResponse } from 'node:http';
import { config } from './config.js';
import { tools, toolByName } from './tools.js';

const SERVER_INFO = { name: 'affine-mcp-ext', version: '1.0.0' };
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

function extractToken(req: IncomingMessage): string | null {
  const auth = req.headers['authorization'];
  if (!auth) return null;
  const m = /^Bearer\s+(.+)$/i.exec(Array.isArray(auth) ? auth[0] : auth);
  return m ? m[1].trim() : null;
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

async function dispatch(req: JsonRpcRequest, token: string): Promise<JsonRpcResult | null> {
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
          const text = await tool.handler(token, args ?? {});
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

const server = http.createServer(async (req, res) => {
  // Health endpoint for Docker healthcheck
  if (req.method === 'GET' && (req.url === '/health' || req.url === '/')) {
    return send(res, 200, { status: 'ok', server: SERVER_INFO });
  }

  if (req.method !== 'POST') {
    return send(res, 405, { error: 'Method Not Allowed — use POST for JSON-RPC' });
  }

  const token = extractToken(req) || config.fallbackToken;
  if (!token) {
    return send(res, 401, {
      jsonrpc: '2.0',
      id: null,
      error: {
        code: -32000,
        message: 'Missing Bearer token. Send Authorization: Bearer <ut_...> header.',
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
    return send(res, 400, {
      jsonrpc: '2.0',
      id: null,
      error: { code: -32700, message: 'Parse error' },
    });
  }

  const response = await dispatch(parsed, token);

  if (response === null) {
    // Notification — acknowledge with 202 No Content
    res.writeHead(202);
    res.end();
    return;
  }

  send(res, 200, response);
});

server.listen(config.port, () => {
  console.log(`[mcp-ext] Listening on :${config.port}`);
  console.log(`[mcp-ext] Proxying AFFiNE at ${config.affineBaseUrl}`);
  console.log(`[mcp-ext] Workspace: ${config.workspaceId}`);
  console.log(`[mcp-ext] Exposing ${tools.length} tools:`);
  for (const t of tools) console.log(`  - ${t.name}`);
});

// Graceful shutdown
for (const sig of ['SIGTERM', 'SIGINT'] as const) {
  process.on(sig, () => {
    console.log(`[mcp-ext] ${sig} received, closing…`);
    server.close(() => process.exit(0));
  });
}
