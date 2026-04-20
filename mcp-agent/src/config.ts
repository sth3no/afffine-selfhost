/**
 * Load configuration from environment variables or .env file.
 */

import { readFileSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';

function loadEnvFile() {
  const envPath = resolve(import.meta.dirname, '..', '.env');
  if (!existsSync(envPath)) return;

  const content = readFileSync(envPath, 'utf-8');
  for (const line of content.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eqIdx = trimmed.indexOf('=');
    if (eqIdx === -1) continue;
    const key = trimmed.slice(0, eqIdx).trim();
    const value = trimmed.slice(eqIdx + 1).trim().replace(/^["']|["']$/g, '');
    if (!process.env[key]) process.env[key] = value;
  }
}

loadEnvFile();

function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    console.error(`Missing required environment variable: ${name}`);
    console.error(`Copy .env.example to .env and fill in your values.`);
    process.exit(1);
  }
  return value;
}

export const config = {
  baseUrl: requireEnv('AFFINE_BASE_URL'),
  workspaceId: requireEnv('AFFINE_WORKSPACE_ID'),
  accessToken: requireEnv('AFFINE_ACCESS_TOKEN'),
  // Optional override for the MCP endpoint URL. If set, the client will POST
  // directly to this URL instead of computing `${baseUrl}/api/workspaces/
  // ${workspaceId}/mcp`. Used when talking to the mcp_ext proxy container,
  // which exposes MCP at a different path (e.g. http://mcp_ext:3100/).
  mcpEndpoint: process.env.AFFINE_MCP_ENDPOINT ?? '',
};
