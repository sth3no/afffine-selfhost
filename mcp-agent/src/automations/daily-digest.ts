/**
 * Daily Digest Automation
 *
 * Creates a daily summary document in your workspace containing:
 * - Recently updated documents
 * - Unread notification count
 * - Unresolved comments across docs
 * - Workspace storage usage
 *
 * Schedule: Run daily at 9 AM
 * Usage: npm run daily-digest
 */

import { config } from '../config.js';
import { AffineMcpClient } from '../mcp-client.js';

export async function runDailyDigest() {
  const client = new AffineMcpClient(
    config.baseUrl,
    config.workspaceId,
    config.accessToken,
    config.mcpEndpoint
  );
  await client.initialize();

  const today = new Date().toISOString().split('T')[0];
  console.log(`[Daily Digest] Generating for ${today}...`);

  // Gather data
  const workspace = await client.getWorkspaceInfo();
  const { total: docCount, documents } = await client.listDocuments(50);
  const members = await client.listMembers();
  const notifs = await client.listNotifications(50);
  const blobs = await client.listBlobs();

  // Find recently updated docs (check each doc's info)
  const recentDocs: Array<{ docId: string; title: string; updatedAt: string }> = [];
  const yesterday = new Date(Date.now() - 24 * 60 * 60 * 1000);

  for (const doc of documents.slice(0, 20)) {
    try {
      const info = await client.getDocumentInfo(doc.docId);
      if (info.createdAt && new Date(info.createdAt) > yesterday) {
        recentDocs.push({
          docId: doc.docId,
          title: info.title || '(untitled)',
          updatedAt: info.createdAt,
        });
      }
    } catch {
      // skip docs we can't access
    }
  }

  // Collect comment counts for top docs
  const commentStats: Array<{ docId: string; title: string; count: number }> = [];
  for (const doc of documents.slice(0, 10)) {
    try {
      const comments = await client.listComments(doc.docId, 1);
      if (comments.total > 0) {
        commentStats.push({
          docId: doc.docId,
          title: doc.title || '(untitled)',
          count: comments.total,
        });
      }
    } catch {
      // skip
    }
  }

  // Build digest markdown
  const lines: string[] = [
    `## Daily Digest: ${today}`,
    '',
    `**Workspace:** ${workspace.name}`,
    `**Members:** ${members.total}`,
    `**Total Documents:** ${docCount}`,
    `**Total Assets:** ${blobs.total}`,
    `**Unread Notifications:** ${notifs.total}`,
    '',
  ];

  if (recentDocs.length > 0) {
    lines.push('### Recently Created Documents', '');
    for (const doc of recentDocs) {
      lines.push(`- **${doc.title}** (${doc.updatedAt})`);
    }
    lines.push('');
  }

  if (commentStats.length > 0) {
    lines.push('### Documents with Comments', '');
    for (const stat of commentStats.sort((a, b) => b.count - a.count)) {
      lines.push(`- **${stat.title}**: ${stat.count} comments`);
    }
    lines.push('');
  }

  if (notifs.notifications.length > 0) {
    lines.push('### Recent Notifications', '');
    for (const n of notifs.notifications.slice(0, 10)) {
      lines.push(`- ${JSON.stringify(n).slice(0, 120)}`);
    }
    lines.push('');
  }

  const markdown = lines.join('\n');

  // Create the digest document
  try {
    const result = await client.createDocument(
      `Daily Digest - ${today}`,
      markdown
    );
    console.log(`[Daily Digest] Created: ${result.docId}`);
  } catch (err) {
    // If create_document is not available (production mode), just print
    console.log('[Daily Digest] Document creation not available (dev/canary only).');
    console.log('[Daily Digest] Output:\n');
    console.log(markdown);
  }

  console.log('[Daily Digest] Done.');
}

// Run if called directly
runDailyDigest().catch(console.error);
