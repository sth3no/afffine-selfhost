/**
 * AFFiNE block construction helpers.
 *
 * Implements the on-disk Y.Doc block schema used by AFFiNE (keys, flavours,
 * version numbers) directly. Reference:
 *   packages/common/native/src/doc_parser/schema.rs
 *   packages/common/native/src/doc_parser/write/builder.rs
 *   packages/common/native/src/doc_parser/write/create.rs
 *
 * We do NOT depend on @blocksuite/store — its version would need to track
 * the self-hosted AFFiNE image. The on-disk schema (key names, flavour
 * strings, version numbers) is what travels over the Yjs sync protocol and
 * has been stable since BlockSuite moved to its current representation.
 *
 * Block layout inside a page doc:
 *
 *     page Y.Doc
 *       "blocks" Y.Map<blockId, Y.Map>:
 *         blockMap:
 *           "sys:id"        → string
 *           "sys:flavour"   → string (e.g. "affine:paragraph")
 *           "sys:version"   → integer
 *           "sys:children"  → Y.Array<blockId>
 *           "prop:*"        → flavour-specific props
 *
 * Rich text lives in Y.Text instances with Quill/Yjs-compatible deltas. An
 * inline doc link is a delta insert with a `reference` attribute:
 *     { insert: " ", attributes: { reference: { type: "LinkedPage", pageId } } }
 */

import * as Y from 'yjs';
import { randomUUID } from 'node:crypto';

export const FLAVOUR_PAGE = 'affine:page';
export const FLAVOUR_SURFACE = 'affine:surface';
export const FLAVOUR_NOTE = 'affine:note';
export const FLAVOUR_PARAGRAPH = 'affine:paragraph';
export const FLAVOUR_LIST = 'affine:list';
export const FLAVOUR_CODE = 'affine:code';
export const FLAVOUR_DIVIDER = 'affine:divider';

const BLOCK_VERSIONS: Record<string, number> = {
  'affine:page': 2,
  'affine:surface': 5,
  'affine:note': 1,
  'affine:paragraph': 1,
  'affine:list': 1,
  'affine:code': 1,
  'affine:divider': 1,
  'affine:image': 1,
  'affine:bookmark': 1,
  'affine:embed-youtube': 1,
  'affine:embed-github': 1,
  'affine:embed-figma': 1,
  'affine:embed-loom': 1,
  'affine:embed-html': 1,
  'affine:embed-iframe': 1,
  'affine:embed-linked-doc': 1,
  'affine:embed-synced-doc': 1,
  'affine:callout': 1,
  'affine:latex': 1,
};

export const SYS_ID = 'sys:id';
export const SYS_FLAVOUR = 'sys:flavour';
export const SYS_VERSION = 'sys:version';
export const SYS_CHILDREN = 'sys:children';

// Alphanumeric id, short enough to match BlockSuite's nanoid-style block IDs.
// BlockSuite uses a custom idgen but accepts any unique string as block id.
const ID_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-';
export function newBlockId(): string {
  const buf = new Uint8Array(10);
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  (globalThis.crypto ?? require('node:crypto').webcrypto).getRandomValues(buf);
  let out = '';
  for (const b of buf) out += ID_ALPHABET[b % ID_ALPHABET.length];
  return out;
}

export function newDocGuid(): string {
  // AFFiNE uses UUID v4 for workspace/doc guids.
  return randomUUID();
}

/** A single inline piece of rich text. */
export type InlineOp =
  | string
  | {
      /** Literal text to insert. */
      text: string;
      /** Quill-style formatting marks. */
      bold?: boolean;
      italic?: boolean;
      underline?: boolean;
      strike?: boolean;
      code?: boolean;
      /** Inline hyperlink URL. */
      link?: string;
      /** Inline doc reference — renders as @DocName pill in AFFiNE. */
      refDocId?: string;
    };

/** Descriptor used by block-level helpers and tools. */
export type BlockSpec =
  | {
      type: 'paragraph';
      /** "text" | "h1" | "h2" | "h3" | "h4" | "h5" | "h6" | "quote" */
      style?: 'text' | 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'h6' | 'quote';
      text: string | InlineOp[];
    }
  | {
      type: 'list';
      /** "bulleted" | "numbered" | "todo" | "toggle" */
      style: 'bulleted' | 'numbered' | 'todo' | 'toggle';
      text: string | InlineOp[];
      checked?: boolean;
    }
  | {
      type: 'code';
      language?: string;
      text: string;
    }
  | { type: 'divider' }
  | {
      /** External URL preview card (any web link). */
      type: 'bookmark';
      url: string;
      caption?: string;
      /** Card layout — defaults to "horizontal". */
      style?: 'vertical' | 'horizontal' | 'list' | 'cube' | 'citation';
    }
  | {
      /** Generic iframe embed (figma, miro, etc. — anything embeddable). */
      type: 'embed-iframe';
      url: string;
      caption?: string;
      title?: string;
      description?: string;
    }
  | {
      /** YouTube video embed. AFFiNE auto-fetches metadata from the URL. */
      type: 'embed-youtube';
      url: string;
      caption?: string;
    }
  | {
      /** GitHub issue / PR card. */
      type: 'embed-github';
      url: string;
      caption?: string;
    }
  | {
      /** Figma file embed. */
      type: 'embed-figma';
      url: string;
      caption?: string;
    }
  | {
      /** Loom video embed. */
      type: 'embed-loom';
      url: string;
      caption?: string;
    }
  | {
      /** Raw HTML embed (sandbox iframe with the given markup). */
      type: 'embed-html';
      html: string;
      caption?: string;
    }
  | {
      /** Card-style link to another doc in this workspace. */
      type: 'embed-linked-doc';
      docId: string;
      style?: 'vertical' | 'horizontal' | 'list' | 'cube' | 'horizontalThin' | 'citation';
      caption?: string;
    }
  | {
      /** Inline-rendered embed of another doc (full content shown). */
      type: 'embed-synced-doc';
      docId: string;
      caption?: string;
    }
  | {
      /** Highlighted callout box with an emoji icon and rich text body. */
      type: 'callout';
      emoji?: string;
      text: string | InlineOp[];
    }
  | {
      /** LaTeX equation block (renders as math). */
      type: 'latex';
      latex: string;
    };

interface Delta {
  insert: string;
  attributes?: Record<string, unknown>;
}

function toDelta(input: string | InlineOp[]): Delta[] {
  if (typeof input === 'string') {
    return input.length > 0 ? [{ insert: input }] : [];
  }
  const out: Delta[] = [];
  for (const op of input) {
    if (typeof op === 'string') {
      if (op.length > 0) out.push({ insert: op });
      continue;
    }
    const { text, bold, italic, underline, strike, code, link, refDocId } = op;
    if (!text) continue;
    const attributes: Record<string, unknown> = {};
    if (bold) attributes.bold = true;
    if (italic) attributes.italic = true;
    if (underline) attributes.underline = true;
    if (strike) attributes.strike = true;
    if (code) attributes.code = true;
    if (link) attributes.link = link;
    if (refDocId) attributes.reference = { type: 'LinkedPage', pageId: refDocId };
    const d: Delta = { insert: text };
    if (Object.keys(attributes).length > 0) d.attributes = attributes;
    out.push(d);
  }
  return out;
}

/** Apply a delta to a Y.Text — inserting text with formatting attributes. */
function applyDelta(text: Y.Text, delta: Delta[]): void {
  // Use Yjs's applyDelta rather than manual insert calls so attributes are
  // encoded identically to how BlockSuite emits them from the editor.
  text.applyDelta(delta as never);
}

/** Create a fresh Y.Text pre-populated with the given delta. */
export function createText(delta: Delta[] = []): Y.Text {
  const t = new Y.Text();
  if (delta.length > 0) applyDelta(t, delta);
  return t;
}

/** Insert the standard sys:* header onto a block map. */
function writeSysFields(block: Y.Map<unknown>, id: string, flavour: string): void {
  block.set(SYS_ID, id);
  block.set(SYS_FLAVOUR, flavour);
  block.set(SYS_VERSION, BLOCK_VERSIONS[flavour] ?? 1);
  block.set(SYS_CHILDREN, new Y.Array<string>());
}

export function getBlocksMap(doc: Y.Doc): Y.Map<Y.Map<unknown>> {
  return doc.getMap<Y.Map<unknown>>('blocks');
}

/** Insert a new block into the doc's blocks map, pre-populated with sys fields. */
function insertBlockMap(doc: Y.Doc, id: string, flavour: string): Y.Map<unknown> {
  const block = new Y.Map<unknown>();
  getBlocksMap(doc).set(id, block);
  writeSysFields(block, id, flavour);
  return block;
}

function childrenArrayOf(block: Y.Map<unknown>): Y.Array<string> {
  const arr = block.get(SYS_CHILDREN);
  if (arr instanceof Y.Array) return arr as Y.Array<string>;
  const fresh = new Y.Array<string>();
  block.set(SYS_CHILDREN, fresh);
  return fresh;
}

/** Create and insert a paragraph/heading/quote block. Returns the new block id. */
export function addParagraphBlock(
  doc: Y.Doc,
  style: 'text' | 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'h6' | 'quote',
  text: string | InlineOp[]
): string {
  const id = newBlockId();
  const block = insertBlockMap(doc, id, FLAVOUR_PARAGRAPH);
  block.set('prop:type', style);
  const t = createText(toDelta(text));
  block.set('prop:text', t);
  return id;
}

/** Create and insert a list block (bulleted / numbered / todo / toggle). */
export function addListBlock(
  doc: Y.Doc,
  style: 'bulleted' | 'numbered' | 'todo' | 'toggle',
  text: string | InlineOp[],
  opts: { checked?: boolean } = {}
): string {
  const id = newBlockId();
  const block = insertBlockMap(doc, id, FLAVOUR_LIST);
  block.set('prop:type', style);
  block.set('prop:text', createText(toDelta(text)));
  if (style === 'todo') block.set('prop:checked', Boolean(opts.checked));
  return id;
}

/** Create and insert a code block. */
export function addCodeBlock(doc: Y.Doc, text: string, language = 'plaintext'): string {
  const id = newBlockId();
  const block = insertBlockMap(doc, id, FLAVOUR_CODE);
  block.set('prop:language', language);
  block.set('prop:text', createText(toDelta(text)));
  return id;
}

/** Create and insert a horizontal divider. */
export function addDividerBlock(doc: Y.Doc): string {
  const id = newBlockId();
  insertBlockMap(doc, id, FLAVOUR_DIVIDER);
  return id;
}

// ── Embeds & rich blocks ─────────────────────────────────────────────

/**
 * Insert a bookmark (URL preview card). AFFiNE's link-preview pipeline
 * fetches og:title / og:description / favicon asynchronously after the
 * block lands; we only need to set the URL and let the FE enrich it.
 */
export function addBookmarkBlock(
  doc: Y.Doc,
  url: string,
  opts: { caption?: string; style?: 'vertical' | 'horizontal' | 'list' | 'cube' | 'citation' } = {}
): string {
  const id = newBlockId();
  const block = insertBlockMap(doc, id, 'affine:bookmark');
  block.set('prop:url', url);
  block.set('prop:style', opts.style ?? 'horizontal');
  block.set('prop:caption', opts.caption ?? null);
  block.set('prop:title', null);
  block.set('prop:description', null);
  block.set('prop:icon', null);
  block.set('prop:image', null);
  block.set('prop:footnoteIdentifier', null);
  // GfxCommonBlockProps defaults — required by the schema even when the
  // block lives inside a note (not on the edgeless surface).
  block.set('prop:index', 'a0');
  block.set('prop:xywh', '[0,0,0,0]');
  block.set('prop:lockedBySelf', false);
  block.set('prop:rotate', 0);
  return id;
}

/**
 * Insert a generic iframe-style embed. The URL the user pasted goes in
 * `prop:url`; AFFiNE's iframe-resolver pipeline derives `prop:iframeUrl`
 * (the actual iframe src) from it asynchronously.
 */
export function addEmbedIframeBlock(
  doc: Y.Doc,
  url: string,
  opts: { caption?: string; title?: string; description?: string } = {}
): string {
  const id = newBlockId();
  const block = insertBlockMap(doc, id, 'affine:embed-iframe');
  block.set('prop:url', url);
  block.set('prop:iframeUrl', '');
  block.set('prop:caption', opts.caption ?? null);
  block.set('prop:title', opts.title ?? null);
  block.set('prop:description', opts.description ?? null);
  block.set('prop:index', 'a0');
  block.set('prop:xywh', '[0,0,0,0]');
  block.set('prop:lockedBySelf', false);
  block.set('prop:scale', 1);
  return id;
}

/**
 * Generic embed-card constructor — used by the URL-preview blocks
 * (youtube/github/figma/loom). Each has flavour-specific URL-data fields
 * (videoId, owner, etc.) that AFFiNE fills in after parsing the URL.
 */
function addEmbedCardBlock(
  doc: Y.Doc,
  flavour: string,
  url: string,
  caption: string | undefined,
  defaultStyle: string,
  extraNullProps: string[]
): string {
  const id = newBlockId();
  const block = insertBlockMap(doc, id, flavour);
  block.set('prop:url', url);
  block.set('prop:style', defaultStyle);
  block.set('prop:caption', caption ?? null);
  for (const k of extraNullProps) block.set(`prop:${k}`, null);
  block.set('prop:index', 'a0');
  block.set('prop:xywh', '[0,0,0,0]');
  block.set('prop:lockedBySelf', false);
  block.set('prop:rotate', 0);
  return id;
}

export function addEmbedYoutubeBlock(doc: Y.Doc, url: string, caption?: string): string {
  return addEmbedCardBlock(doc, 'affine:embed-youtube', url, caption, 'video', [
    'videoId',
    'image',
    'title',
    'description',
    'creator',
    'creatorUrl',
    'creatorImage',
  ]);
}

export function addEmbedGithubBlock(doc: Y.Doc, url: string, caption?: string): string {
  // owner/repo/githubType/githubId are filled in by AFFiNE's GitHub-link
  // resolver after the block is created — we just need the URL and the
  // structural fields the schema requires.
  const id = newBlockId();
  const block = insertBlockMap(doc, id, 'affine:embed-github');
  block.set('prop:url', url);
  block.set('prop:style', 'horizontal');
  block.set('prop:caption', caption ?? null);
  block.set('prop:owner', '');
  block.set('prop:repo', '');
  block.set('prop:githubType', 'issue');
  block.set('prop:githubId', '');
  block.set('prop:image', null);
  block.set('prop:title', null);
  block.set('prop:description', null);
  block.set('prop:status', null);
  block.set('prop:statusReason', null);
  block.set('prop:index', 'a0');
  block.set('prop:xywh', '[0,0,0,0]');
  block.set('prop:lockedBySelf', false);
  block.set('prop:rotate', 0);
  return id;
}

export function addEmbedFigmaBlock(doc: Y.Doc, url: string, caption?: string): string {
  return addEmbedCardBlock(doc, 'affine:embed-figma', url, caption, 'horizontal', [
    'image',
    'title',
    'description',
  ]);
}

export function addEmbedLoomBlock(doc: Y.Doc, url: string, caption?: string): string {
  return addEmbedCardBlock(doc, 'affine:embed-loom', url, caption, 'video', [
    'videoId',
    'image',
    'title',
    'description',
  ]);
}

/** Raw HTML iframe embed — runs the markup in a sandboxed iframe. */
export function addEmbedHtmlBlock(doc: Y.Doc, html: string, caption?: string): string {
  const id = newBlockId();
  const block = insertBlockMap(doc, id, 'affine:embed-html');
  block.set('prop:html', html);
  block.set('prop:style', 'html');
  block.set('prop:caption', caption ?? null);
  block.set('prop:design', '');
  block.set('prop:index', 'a0');
  block.set('prop:xywh', '[0,0,0,0]');
  block.set('prop:lockedBySelf', false);
  block.set('prop:rotate', 0);
  return id;
}

/** Card-style link to another workspace doc — renders as a preview pill. */
export function addEmbedLinkedDocBlock(
  doc: Y.Doc,
  pageId: string,
  opts: {
    style?: 'vertical' | 'horizontal' | 'list' | 'cube' | 'horizontalThin' | 'citation';
    caption?: string;
  } = {}
): string {
  const id = newBlockId();
  const block = insertBlockMap(doc, id, 'affine:embed-linked-doc');
  block.set('prop:pageId', pageId);
  // ReferenceInfo: optional `params` describes which mode/blocks to deep-link
  // into. For a plain card link an empty {mode:'page'} is enough.
  const params = new Y.Map<unknown>();
  params.set('mode', 'page');
  block.set('prop:params', params);
  block.set('prop:style', opts.style ?? 'horizontal');
  block.set('prop:caption', opts.caption ?? null);
  block.set('prop:footnoteIdentifier', null);
  block.set('prop:index', 'a0');
  block.set('prop:xywh', '[0,0,0,0]');
  block.set('prop:lockedBySelf', false);
  block.set('prop:rotate', 0);
  return id;
}

/** Embed-mode reference to another doc — full inline render of its content. */
export function addEmbedSyncedDocBlock(doc: Y.Doc, pageId: string, caption?: string): string {
  const id = newBlockId();
  const block = insertBlockMap(doc, id, 'affine:embed-synced-doc');
  block.set('prop:pageId', pageId);
  const params = new Y.Map<unknown>();
  params.set('mode', 'page');
  block.set('prop:params', params);
  block.set('prop:style', 'syncedDoc');
  block.set('prop:caption', caption ?? null);
  block.set('prop:scale', 1);
  block.set('prop:index', 'a0');
  block.set('prop:xywh', '[0,0,0,0]');
  block.set('prop:lockedBySelf', false);
  block.set('prop:rotate', 0);
  return id;
}

/** Callout box with an emoji + rich text body. Children may be added later. */
export function addCalloutBlock(
  doc: Y.Doc,
  text: string | InlineOp[],
  emoji = '😀'
): string {
  const id = newBlockId();
  const block = insertBlockMap(doc, id, 'affine:callout');
  block.set('prop:emoji', emoji);
  block.set('prop:text', createText(toDelta(text)));
  return id;
}

/** LaTeX equation block — renders mathematical notation. */
export function addLatexBlock(doc: Y.Doc, latex: string): string {
  const id = newBlockId();
  const block = insertBlockMap(doc, id, 'affine:latex');
  block.set('prop:latex', latex);
  // GfxCommonBlockProps — required even for note-embedded latex.
  block.set('prop:xywh', '[0,0,16,16]');
  block.set('prop:index', 'a0');
  block.set('prop:lockedBySelf', false);
  block.set('prop:scale', 1);
  block.set('prop:rotate', 0);
  return id;
}

/**
 * Create a block from a BlockSpec and return its new id. Doesn't attach it
 * to any parent — caller is responsible for pushing the id into a
 * sys:children array.
 */
export function addBlockFromSpec(doc: Y.Doc, spec: BlockSpec): string {
  switch (spec.type) {
    case 'paragraph':
      return addParagraphBlock(doc, spec.style ?? 'text', spec.text);
    case 'list':
      return addListBlock(doc, spec.style, spec.text, { checked: spec.checked });
    case 'code':
      return addCodeBlock(doc, spec.text, spec.language);
    case 'divider':
      return addDividerBlock(doc);
    case 'bookmark':
      return addBookmarkBlock(doc, spec.url, { caption: spec.caption, style: spec.style });
    case 'embed-iframe':
      return addEmbedIframeBlock(doc, spec.url, {
        caption: spec.caption,
        title: spec.title,
        description: spec.description,
      });
    case 'embed-youtube':
      return addEmbedYoutubeBlock(doc, spec.url, spec.caption);
    case 'embed-github':
      return addEmbedGithubBlock(doc, spec.url, spec.caption);
    case 'embed-figma':
      return addEmbedFigmaBlock(doc, spec.url, spec.caption);
    case 'embed-loom':
      return addEmbedLoomBlock(doc, spec.url, spec.caption);
    case 'embed-html':
      return addEmbedHtmlBlock(doc, spec.html, spec.caption);
    case 'embed-linked-doc':
      return addEmbedLinkedDocBlock(doc, spec.docId, { style: spec.style, caption: spec.caption });
    case 'embed-synced-doc':
      return addEmbedSyncedDocBlock(doc, spec.docId, spec.caption);
    case 'callout':
      return addCalloutBlock(doc, spec.text, spec.emoji);
    case 'latex':
      return addLatexBlock(doc, spec.latex);
  }
}

/** Append children to a block's sys:children Y.Array. */
export function appendChildren(parent: Y.Map<unknown>, childIds: string[]): void {
  const arr = childrenArrayOf(parent);
  arr.push(childIds);
}

/** Insert children at a specific position in sys:children. */
export function insertChildrenAt(parent: Y.Map<unknown>, index: number, childIds: string[]): void {
  const arr = childrenArrayOf(parent);
  arr.insert(index, childIds);
}

/** Initialize a blank AFFiNE page doc (page → surface + note → empty). */
export interface FreshDocIds {
  pageId: string;
  surfaceId: string;
  noteId: string;
}

export function initializeEmptyDoc(doc: Y.Doc, title: string): FreshDocIds {
  const pageId = newBlockId();
  const surfaceId = newBlockId();
  const noteId = newBlockId();

  // Page block
  const page = insertBlockMap(doc, pageId, FLAVOUR_PAGE);
  page.set('prop:title', createText(title ? [{ insert: title }] : []));
  appendChildren(page, [surfaceId, noteId]);

  // Surface block (empty — canvas surface used for whiteboard elements)
  const surface = insertBlockMap(doc, surfaceId, FLAVOUR_SURFACE);
  const elements = new Y.Map<unknown>();
  elements.set('type', '$blocksuite:internal:native$');
  elements.set('value', new Y.Map<unknown>());
  surface.set('prop:elements', elements);

  // Note block (wraps all real content)
  const note = insertBlockMap(doc, noteId, FLAVOUR_NOTE);
  const background = new Y.Map<unknown>();
  background.set('light', '#ffffff');
  background.set('dark', '#252525');
  note.set('prop:background', background);
  note.set('prop:xywh', '[0,0,800,95]');
  note.set('prop:index', 'a0');
  note.set('prop:hidden', false);
  note.set('prop:displayMode', 'both');

  return { pageId, surfaceId, noteId };
}

/** Find the page block id (singular — each AFFiNE doc has exactly one). */
export function findPageBlockId(doc: Y.Doc): string | null {
  const blocks = getBlocksMap(doc);
  for (const [id, block] of blocks) {
    if (block.get(SYS_FLAVOUR) === FLAVOUR_PAGE) return id;
  }
  return null;
}

/** Find the first note block (where content lives). */
export function findFirstNoteBlockId(doc: Y.Doc): string | null {
  const blocks = getBlocksMap(doc);
  for (const [id, block] of blocks) {
    if (block.get(SYS_FLAVOUR) === FLAVOUR_NOTE) return id;
  }
  return null;
}

/** Extract plain text from a block (title or text prop). */
export function blockTextPreview(block: Y.Map<unknown>, maxLen = 120): string {
  for (const key of ['prop:title', 'prop:text']) {
    const v = block.get(key);
    if (v instanceof Y.Text) {
      const s = v.toString();
      return s.length > maxLen ? s.slice(0, maxLen) + '…' : s;
    }
  }
  return '';
}

/** Read a doc's title (from the page block's prop:title). */
export function readDocTitle(doc: Y.Doc): string {
  const pageId = findPageBlockId(doc);
  if (!pageId) return '';
  const page = getBlocksMap(doc).get(pageId)!;
  const title = page.get('prop:title');
  return title instanceof Y.Text ? title.toString() : '';
}

/**
 * Flat list of (id, flavour, preview) of every block in the doc — useful for
 * agents that need to pick a heading to append after.
 */
export interface BlockInfo {
  id: string;
  flavour: string;
  style?: string;
  text: string;
  /** Indentation depth in the block tree (0 = page block). */
  depth?: number;
}

export function listBlocks(doc: Y.Doc): BlockInfo[] {
  // Tree walk from the page block via sys:children, so agents see the
  // actual document order (not the internal Y.Map insertion order, which
  // is stable but not semantically meaningful).
  const blocks = getBlocksMap(doc);
  const pageId = findPageBlockId(doc);
  const out: BlockInfo[] = [];
  const visited = new Set<string>();

  function visit(id: string, depth: number): void {
    if (visited.has(id)) return;
    visited.add(id);
    const block = blocks.get(id);
    if (!block) return;
    const flavour = String(block.get(SYS_FLAVOUR) ?? '');
    const style = block.get('prop:type');
    out.push({
      id,
      flavour,
      style: typeof style === 'string' ? style : undefined,
      text: blockTextPreview(block),
      depth,
    } as BlockInfo & { depth: number });
    const children = block.get(SYS_CHILDREN);
    if (children instanceof Y.Array) {
      for (const cid of (children as Y.Array<string>).toArray()) visit(cid, depth + 1);
    }
  }

  if (pageId) visit(pageId, 0);
  // Append any orphan blocks not reachable from the page (defensive).
  for (const [id] of blocks) if (!visited.has(id)) visit(id, 0);
  return out;
}

/**
 * Find the note block that contains a given block id in its sys:children.
 * Used when inserting a new block next to an existing one.
 */
export function findParentBlockId(doc: Y.Doc, childId: string): string | null {
  for (const [id, block] of getBlocksMap(doc)) {
    const children = block.get(SYS_CHILDREN);
    if (children instanceof Y.Array) {
      const arr = (children as Y.Array<string>).toArray();
      if (arr.includes(childId)) return id;
    }
  }
  return null;
}

/** Find a heading block whose plain text matches (case-insensitive, trimmed). */
export function findHeadingByText(doc: Y.Doc, query: string): string | null {
  const needle = query.trim().toLowerCase();
  for (const [id, block] of getBlocksMap(doc)) {
    if (block.get(SYS_FLAVOUR) !== FLAVOUR_PARAGRAPH) continue;
    const style = block.get('prop:type');
    if (typeof style !== 'string' || !/^h[1-6]$/.test(style)) continue;
    if (blockTextPreview(block).trim().toLowerCase() === needle) return id;
  }
  return null;
}
