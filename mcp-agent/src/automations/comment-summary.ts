/**
 * Comment Summary Automation
 *
 * Scans all documents for unresolved comments and creates a summary.
 * Useful for team leads who need to track open feedback.
 *
 * Schedule: Run daily at 8 AM
 * Usage: npm run comment-summary
 */

import { config } from '../config.js';
import { AffineMcpClient } from '../mcp-client.js';

export async function runCommentSummary() {
  const client = new AffineMcpClient(
    config.baseUrl,
    config.workspaceId,
    config.accessToken,
    config.mcpEndpoint
  );
  await client.initialize();

  console.log('[Comment Summary] Scanning workspace for comments...');

  const { documents } = await client.listDocuments(100);
  const docsWithComments: Array<{
    docId: string;
    title: string;
    commentCount: number;
    comments: Array<{ id: string; content: unknown; user: unknown; createdAt: string }>;
  }> = [];

  for (const doc of documents) {
    try {
      const result = await client.listComments(doc.docId, 50);
      if (result.total > 0) {
        docsWithComments.push({
          docId: doc.docId,
          title: doc.title || '(untitled)',
          commentCount: result.total,
          comments: result.comments.slice(0, 10),
        });
      }
    } catch {
      // skip inaccessible docs
    }
  }

  const totalComments = docsWithComments.reduce((sum, d) => sum + d.commentCount, 0);

  console.log(`[Comment Summary] ${totalComments} comments across ${docsWithComments.length} documents.\n`);

  if (docsWithComments.length === 0) {
    console.log('  No comments found in any documents.');
    return;
  }

  docsWithComments.sort((a, b) => b.commentCount - a.commentCount);

  for (const doc of docsWithComments) {
    console.log(`  "${doc.title}" — ${doc.commentCount} comments`);
  }

  // Build summary markdown
  const today = new Date().toISOString().split('T')[0];
  const lines = [
    `## Comment Summary - ${today}`,
    '',
    `**Total Comments:** ${totalComments}`,
    `**Documents with Comments:** ${docsWithComments.length}`,
    '',
    '### Breakdown by Document',
    '',
    ...docsWithComments.map(
      d => `- **${d.title}** — ${d.commentCount} comments`
    ),
  ];

  try {
    const result = await client.createDocument(
      `Comment Summary - ${today}`,
      lines.join('\n')
    );
    console.log(`\n[Comment Summary] Report created: ${result.docId}`);
  } catch {
    console.log('\n[Comment Summary] Full report:\n');
    console.log(lines.join('\n'));
  }
}

runCommentSummary().catch(console.error);
