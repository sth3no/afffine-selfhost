/**
 * Environment configuration for the MCP extension proxy.
 *
 * All values come from env vars injected by Portainer at container start.
 * The proxy refuses to start without them:
 *
 *   MCP_EXT_HTTP_TOKEN   shared secret clients must send as
 *                        `Authorization: Bearer <token>` (never forwarded)
 *   AFFINE_MCP_EMAIL     AFFiNE account mcp-ext signs in as (AFFiNE 0.27
 *   AFFINE_MCP_PASSWORD  removed personal access tokens) — use a dedicated
 *                        account that is a member/editor of the workspace
 */

function required(name: string): string {
  const v = process.env[name];
  if (!v || !v.trim()) {
    console.error(`[mcp-ext] Missing required env var: ${name}`);
    process.exit(1);
  }
  return v;
}

function optional(name: string): string | undefined {
  const v = process.env[name]?.trim();
  return v ? v : undefined;
}

export const config = {
  // AFFiNE base URL (internal Docker DNS, e.g. http://affine:3010)
  affineBaseUrl: required('AFFINE_BASE_URL').replace(/\/$/, ''),

  // Workspace this proxy is scoped to. The proxy is deployed per-workspace;
  // for multi-workspace setups, run multiple proxy containers.
  workspaceId: required('AFFINE_WORKSPACE_ID'),

  // Client → proxy shared secret.
  httpToken: required('MCP_EXT_HTTP_TOKEN').trim(),

  // Proxy → AFFiNE service account (cookie session, kept in memory only).
  affineEmail: required('AFFINE_MCP_EMAIL').trim(),
  affinePassword: required('AFFINE_MCP_PASSWORD'),

  // Sent as x-affine-version. Only matters when AFFiNE's client version
  // control is enabled (client.versionControl.enabled, off by default).
  affineClientVersion: optional('AFFINE_CLIENT_VERSION') ?? '0.27.4',

  // Name of the native-MCP credential this instance creates and manages.
  // Leftover credentials of the same account with this name are revoked —
  // give every mcp-ext instance that shares an AFFiNE account its own name.
  mcpCredentialName: optional('MCP_EXT_CREDENTIAL_NAME') ?? 'affine-mcp-ext',

  // Port the MCP HTTP server listens on (internal to affine_net).
  port: Number(process.env.PORT ?? 3100),
};

if (config.httpToken.length < 32) {
  console.warn('[mcp-ext] MCP_EXT_HTTP_TOKEN is shorter than 32 characters — generate one with `openssl rand -hex 32`.');
}

/** Build the URL to AFFiNE's native MCP endpoint. */
export function nativeMcpUrl(): string {
  return `${config.affineBaseUrl}/api/workspaces/${config.workspaceId}/mcp`;
}
