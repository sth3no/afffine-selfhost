/**
 * Automation Scheduler
 *
 * Runs all automations on their configured schedules using node-cron.
 * Keep this process running (e.g., via PM2, systemd, or Docker).
 *
 * Usage: npm run scheduler
 *
 * Schedules:
 *   - Daily Digest:    Every day at 9:00 AM
 *   - Comment Summary: Every day at 8:00 AM
 *   - Stale Docs:      Every Monday at 10:00 AM
 */

import cron from 'node-cron';
import { runDailyDigest } from './automations/daily-digest.js';
import { runCommentSummary } from './automations/comment-summary.js';
import { runStaleDocs } from './automations/stale-docs.js';
import { AffineMcpClient } from './mcp-client.js';
import { config } from './config.js';

/**
 * Query AFFiNE once at startup to log which MCP tools are actually exposed.
 * Different AFFiNE versions (stable / beta / canary) expose different tool
 * sets — this diagnostic makes mismatches obvious in the container logs
 * instead of showing up later as cryptic "tool not found" errors.
 */
async function logAvailableTools() {
  try {
    const client = new AffineMcpClient(
      config.baseUrl,
      config.workspaceId,
      config.accessToken,
      config.mcpEndpoint
    );
    await client.initialize();
    const tools = await client.listTools();
    console.log(`[Scheduler] AFFiNE exposes ${tools.length} MCP tools:`);
    for (const t of tools) {
      console.log(`  - ${t.name}${t.title ? ` (${t.title})` : ''}`);
    }
  } catch (err) {
    console.error('[Scheduler] Could not enumerate MCP tools:', err);
  }
}

function wrap(name: string, fn: () => Promise<void>) {
  return async () => {
    const start = Date.now();
    console.log(`\n${'='.repeat(60)}`);
    console.log(`[Scheduler] Starting "${name}" at ${new Date().toISOString()}`);
    console.log('='.repeat(60));
    try {
      await fn();
      console.log(`[Scheduler] "${name}" completed in ${Date.now() - start}ms`);
    } catch (err) {
      console.error(`[Scheduler] "${name}" failed:`, err);
    }
  };
}

// ── Schedule definitions ────────────────────────────────────────

// Daily at 8:00 AM — Comment Summary
cron.schedule('0 8 * * *', wrap('Comment Summary', runCommentSummary));

// Daily at 9:00 AM — Daily Digest
cron.schedule('0 9 * * *', wrap('Daily Digest', runDailyDigest));

// Monday at 10:00 AM — Stale Docs Report
cron.schedule('0 10 * * 1', wrap('Stale Docs', runStaleDocs));

console.log('[Scheduler] AFFiNE MCP Automation Scheduler started.');
console.log('[Scheduler] Registered jobs:');
console.log('  - Comment Summary:  daily at 08:00');
console.log('  - Daily Digest:     daily at 09:00');
console.log('  - Stale Docs:       Mondays at 10:00');

// Fire-and-forget diagnostic — doesn't block scheduler startup
logAvailableTools().then(() =>
  console.log('[Scheduler] Waiting for next scheduled run...\n')
);
