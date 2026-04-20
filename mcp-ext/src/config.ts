/**
 * Environment configuration for the MCP extension proxy.
 *
 * All values come from env vars injected by Portainer at container start.
 * AFFINE_ACCESS_TOKEN is optional at startup — if absent, the proxy starts
 * but tools will fail until the client provides a Bearer token in the
 * request Authorization header (which we prefer over the env var anyway,
 * so Bring-Your-Own-Token clients work).
 */

function required(name: string): string {
  const v = process.env[name];
  if (!v) {
    console.error(`[mcp-ext] Missing required env var: ${name}`);
    process.exit(1);
  }
  return v;
}

export const config = {
  // AFFiNE base URL (internal Docker DNS, e.g. http://affine:3010)
  affineBaseUrl: required('AFFINE_BASE_URL').replace(/\/$/, ''),

  // Workspace this proxy is scoped to. The proxy is deployed per-workspace;
  // for multi-workspace setups, run multiple proxy containers.
  workspaceId: required('AFFINE_WORKSPACE_ID'),

  // Optional fallback token. Used only if a request doesn't carry its own
  // Authorization: Bearer header.
  fallbackToken: process.env.AFFINE_ACCESS_TOKEN ?? '',

  // Port the MCP HTTP server listens on (internal to affine_net).
  port: Number(process.env.PORT ?? 3100),
};

/** Build the URL to AFFiNE's native MCP endpoint. */
export function nativeMcpUrl(): string {
  return `${config.affineBaseUrl}/api/workspaces/${config.workspaceId}/mcp`;
}

/** Build the URL to AFFiNE's GraphQL endpoint. */
export function graphqlUrl(): string {
  return `${config.affineBaseUrl}/graphql`;
}
