/**
 * Interactive AFFiNE MCP Agent
 *
 * Run this to test your connection and explore available tools.
 *
 * Usage: npm run start
 */

import { config } from './config.js';
import { AffineMcpClient } from './mcp-client.js';

async function main() {
  const client = new AffineMcpClient(
    config.baseUrl,
    config.workspaceId,
    config.accessToken,
    config.mcpEndpoint
  );

  console.log('Connecting to AFFiNE MCP server...');
  console.log(`  Endpoint: ${client.endpoint}`);
  console.log();

  // Initialize session
  await client.initialize();
  console.log('Session initialized.\n');

  // List available tools
  const tools = await client.listTools();
  console.log(`Available tools (${tools.length}):`);
  for (const tool of tools) {
    console.log(`  - ${tool.name}: ${tool.description.slice(0, 80)}...`);
  }
  console.log();

  // Get workspace info
  const workspace = await client.getWorkspaceInfo();
  console.log(`Workspace: ${workspace.name} (team: ${workspace.isTeam})`);
  console.log();

  // List documents
  const { total, documents } = await client.listDocuments(5);
  console.log(`Documents (${total} total, showing first 5):`);
  for (const doc of documents) {
    console.log(`  - [${doc.docId}] ${doc.title || '(untitled)'}`);
  }
  console.log();

  // List members
  const members = await client.listMembers();
  console.log(`Members (${members.total}):`);
  for (const m of members.members) {
    console.log(`  - ${m.name || m.email} (${m.role})`);
  }
  console.log();

  // Check notifications
  const notifs = await client.listNotifications(5);
  console.log(`Notifications (${notifs.total} total):`);
  if (notifs.notifications.length === 0) {
    console.log('  (none)');
  }
  for (const n of notifs.notifications) {
    console.log(`  - ${JSON.stringify(n).slice(0, 100)}...`);
  }

  console.log('\nAgent connected and ready.');
}

main().catch(err => {
  console.error('Failed to connect:', err.message);
  process.exit(1);
});
