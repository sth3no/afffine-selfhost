/**
 * Blob upload tool — uploads a binary file (image, video, attachment) to
 * the workspace's blob storage via AFFiNE's GraphQL `setBlob` mutation.
 *
 * Uses the GraphQL multipart-request spec
 * (https://github.com/jaydenseric/graphql-multipart-request-spec) which
 * AFFiNE accepts via @nestjs/graphql + graphql-upload. AFFiNE returns
 * the uploaded filename; that filename IS the sourceId you pass to
 * `affine:image` / `affine:attachment` block props.
 *
 * The caller passes base64-encoded bytes (so the tool input is plain
 * JSON-safe text). We decode + repackage as multipart on the wire.
 *
 * Workspace quota errors (BlobQuotaExceeded, StorageQuotaExceeded) bubble
 * up as MCP tool errors with the original GraphQL message.
 */

import { config, graphqlUrl } from './config.js';
import type { ToolDefinition } from './tools-shared.js';

const SET_BLOB_MUTATION = `
  mutation SetBlob($workspaceId: String!, $blob: Upload!) {
    setBlob(workspaceId: $workspaceId, blob: $blob)
  }
`;

const uploadBlob: ToolDefinition = {
  name: 'upload_blob',
  description:
    'Upload a binary file (image, audio, video, attachment) to the workspace ' +
    'blob storage. Returns { sourceId } — the value you pass as prop:sourceId on ' +
    'an affine:image or affine:attachment block. The filename you provide becomes ' +
    'the sourceId so make it unique (suggestion: prefix with capture id).',
  inputSchema: {
    type: 'object',
    properties: {
      filename: {
        type: 'string',
        description: 'Unique filename used as the blob id. Include a sensible extension (.jpg, .png, .pdf).',
      },
      contentType: {
        type: 'string',
        description: 'MIME type, e.g. image/jpeg, image/png, application/pdf, video/mp4.',
      },
      base64: {
        type: 'string',
        description: 'Base64-encoded file contents.',
      },
    },
    required: ['filename', 'contentType', 'base64'],
  },
  async handler(token, args) {
    const filename = String(args.filename ?? '').trim();
    const contentType = String(args.contentType ?? '').trim();
    const b64 = String(args.base64 ?? '');
    if (!filename || !contentType || !b64) {
      throw new Error('filename, contentType, and base64 are all required');
    }

    let bytes: Buffer;
    try {
      bytes = Buffer.from(b64, 'base64');
    } catch (e) {
      throw new Error(`base64 decode failed: ${e instanceof Error ? e.message : String(e)}`);
    }
    if (bytes.length === 0) {
      throw new Error('decoded blob is 0 bytes');
    }

    const sourceId = await postSetBlob(token, filename, contentType, bytes);
    return JSON.stringify({ sourceId, byteCount: bytes.length, ok: true }, null, 2);
  },
};

async function postSetBlob(
  token: string,
  filename: string,
  contentType: string,
  bytes: Buffer,
): Promise<string> {
  // Build a graphql-multipart-request:
  //   - "operations" — JSON with the query, variables (blob field is null placeholder)
  //   - "map" — JSON: which form-data parts map to which variable paths
  //   - "0" — the actual file part
  const operations = JSON.stringify({
    query: SET_BLOB_MUTATION,
    variables: { workspaceId: config.workspaceId, blob: null },
  });
  const map = JSON.stringify({ '0': ['variables.blob'] });

  const formData = new FormData();
  formData.append('operations', operations);
  formData.append('map', map);
  // Buffer → Uint8Array for the Blob constructor — TypeScript's strict
  // ArrayBuffer typing rejects Node's Buffer<ArrayBufferLike> here.
  formData.append(
    '0',
    new Blob([new Uint8Array(bytes)], { type: contentType }),
    filename,
  );

  const res = await fetch(graphqlUrl(), {
    method: 'POST',
    headers: {
      // Don't set Content-Type — fetch+FormData picks the right boundary.
      Authorization: `Bearer ${token}`,
      // graphql-multipart-request-spec recommends this header to opt out of
      // potential CSRF protections on POST.
      'apollo-require-preflight': 'true',
    },
    body: formData,
  });

  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(
      `setBlob HTTP ${res.status} ${res.statusText}${text ? ` — ${text.slice(0, 400)}` : ''}`,
    );
  }

  const body = (await res.json()) as {
    data?: { setBlob?: string };
    errors?: Array<{ message: string }>;
  };
  if (body.errors && body.errors.length > 0) {
    throw new Error(
      `setBlob GraphQL error: ${body.errors.map(e => e.message).join('; ')}`,
    );
  }
  if (!body.data?.setBlob) {
    throw new Error('setBlob returned no sourceId');
  }
  return body.data.setBlob;
}

export const blobTools: ToolDefinition[] = [uploadBlob];
