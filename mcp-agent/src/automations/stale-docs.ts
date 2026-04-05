/**
 * Stale Document Detector
 *
 * Finds documents that haven't been updated in a configurable number of days
 * and creates a report. Useful for keeping workspaces clean and identifying
 * abandoned drafts.
 *
 * Schedule: Run weekly on Monday at 10 AM
 * Usage: npm run stale-docs
 */

import { config } from '../config.js';
import { AffineMcpClient } from '../mcp-client.js';

const STALE_THRESHOLD_DAYS = 30;

export async function runStaleDocs() {
  const client = new AffineMcpClient(
    config.baseUrl,
    config.workspaceId,
    config.accessToken
  );
  await client.initialize();

  console.log(`[Stale Docs] Finding documents not updated in ${STALE_THRESHOLD_DAYS}+ days...`);

  const { documents } = await client.listDocuments(100);
  const threshold = new Date(Date.now() - STALE_THRESHOLD_DAYS * 24 * 60 * 60 * 1000);
  const staleDocs: Array<{
    docId: string;
    title: string;
    createdAt: string;
    daysSinceUpdate: number;
  }> = [];

  for (const doc of documents) {
    try {
      const info = await client.getDocumentInfo(doc.docId);
      const updatedAt = new Date(info.createdAt || info.updatedAt || 0);
      if (updatedAt < threshold) {
        const daysSince = Math.floor(
          (Date.now() - updatedAt.getTime()) / (24 * 60 * 60 * 1000)
        );
        staleDocs.push({
          docId: doc.docId,
          title: info.title || '(untitled)',
          createdAt: updatedAt.toISOString().split('T')[0],
          daysSinceUpdate: daysSince,
        });
      }
    } catch {
      // skip inaccessible docs
    }
  }

  staleDocs.sort((a, b) => b.daysSinceUpdate - a.daysSinceUpdate);

  console.log(`[Stale Docs] Found ${staleDocs.length} stale documents:\n`);

  if (staleDocs.length === 0) {
    console.log('  All documents are up to date!');
    return;
  }

  for (const doc of staleDocs) {
    console.log(
      `  - "${doc.title}" — ${doc.daysSinceUpdate} days stale (last: ${doc.createdAt})`
    );
  }

  // Build report markdown
  const today = new Date().toISOString().split('T')[0];
  const lines = [
    `## Stale Documents Report - ${today}`,
    '',
    `Found **${staleDocs.length}** documents not updated in ${STALE_THRESHOLD_DAYS}+ days.`,
    '',
    '| Document | Days Stale | Last Updated |',
    '|----------|-----------|-------------|',
    ...staleDocs.map(
      d => `| ${d.title} | ${d.daysSinceUpdate} | ${d.createdAt} |`
    ),
    '',
    'Consider reviewing these documents for archival or deletion.',
  ];

  try {
    const result = await client.createDocument(
      `Stale Docs Report - ${today}`,
      lines.join('\n')
    );
    console.log(`\n[Stale Docs] Report created: ${result.docId}`);
  } catch {
    console.log('\n[Stale Docs] Full report:\n');
    console.log(lines.join('\n'));
  }
}

runStaleDocs().catch(console.error);
