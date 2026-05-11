"""Markdown → AFFiNE block-spec emitter.

Uses `markdown-it-py` for CommonMark parsing. Adds project-specific
syntax handled inline:
  - Fenced code blocks with language sentinel `embed-html` → embed-html block
  - Image refs `![alt](kf:<n>)` → image block backed by keyframe blob_source_id
  - Cross-doc refs `[[Doc Title]]` → embed-linked-doc block (async MCP call)
  - Callout blocks `> [!callout] text` (single or multi-line) → affine:callout

Inline formatting (**bold**, _italic_, ~~strike~~, `code`, [label](url)) is
parsed via markdown-it's inline tokenizer and emitted as AFFiNE InlineOp[]
arrays so the rendered doc shows proper rich-text instead of literal
asterisks/underscores/backticks.

Async because cross-doc resolution hits the MCP server.

Returns a list of block-spec dicts in the shape consumed by
mcp_ext's append_blocks tool (see [`mcp-ext/src/write-tools.ts`]).
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

from markdown_it import MarkdownIt
from markdown_it.token import Token

log = logging.getLogger(__name__)

_KF_REF_RE = re.compile(r"^kf:(\d+)$")
_CROSS_DOC_RE = re.compile(r"\[\[([^\]]+)\]\]")
_INLINE_LINK_RE = re.compile(
    r"\[(?P<label>[^\]]*)\]\((?P<url>[^)\s]+)\)"
)
_IMAGE_REF_RE = re.compile(r"^!\[(?P<alt>[^\]]*)\]\((?P<src>[^)\s]+)\)\s*$")

# Captures `kf:N` references INSIDE `![alt](kf:N)` markdown image syntax.
# Tolerant of any alt text and any surrounding context.
_COUNT_KF_REF_RE = re.compile(r"!\[[^\]]*\]\(kf:(\d+)\)")


def count_keyframe_refs(body_md: str) -> set[int]:
    """Return the set of integer indices referenced via `![cap](kf:N)`
    image syntax in the body_md. Used by the orchestrator to decide
    whether to append a `## Keyframes` fallback section when the template
    didn't surface any keyframes itself."""
    return {int(m.group(1)) for m in _COUNT_KF_REF_RE.finditer(body_md or "")}

# Multi-line callout pattern: `> [!callout]` line plus any contiguous
# `> body` continuation lines. The head is whatever follows `[!callout]`
# on the same line (often empty when the LLM writes body on next lines).
_CALLOUT_BLOCK_RE = re.compile(
    r"^[ \t]*>[ \t]*\[!callout\][ \t]*(?P<head>[^\n]*)"
    r"(?P<cont>(?:\n[ \t]*>[^\n]*)*)",
    re.MULTILINE,
)

# Unique placeholder for callouts pre-extracted before markdown-it sees the
# body_md. Plain alphanumeric so markdown-it tokenizes it as text and we
# can find it in a post-pass. Index is appended.
_CALLOUT_PLACEHOLDER_PREFIX = "AFFINECALLOUTPLACEHOLDER"

# Shared inline tokenizer for parsing rich-text content (bold/italic/code/
# links) inside paragraphs, headings, list items, blockquotes.
_INLINE_PARSER = MarkdownIt("commonmark", {"breaks": False, "html": False})


# ── Public entry point ──────────────────────────────────────────────


async def markdown_to_blocks(
    md: str,
    *,
    keyframes: list[dict[str, Any]],
    mcp_client: Any | None,
) -> list[dict[str, Any]]:
    """Parse `md` and emit AFFiNE block specs.

    `keyframes` resolves `kf:<n>` image refs to image blocks with the
    n-th keyframe's `blob_source_id` and caption.
    `mcp_client` resolves `[[Doc Title]]` refs via `find_doc_by_title`.
    Pass None to skip cross-doc resolution (refs render as plain text).
    """
    md, callout_bodies = _extract_callouts(md)

    parser = MarkdownIt("commonmark", {"breaks": False, "html": False})
    tokens = parser.parse(md)

    blocks: list[dict[str, Any]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]

        # Headings
        if tok.type == "heading_open":
            level = int(tok.tag[1])  # h1..h6 → 1..6
            inline = tokens[i + 1]
            blocks.append({
                "type": "paragraph",
                "style": f"h{level}",
                "text": _walk_inline_children(inline.children or [])
                        or inline.content,
            })
            i += 3  # heading_open, inline, heading_close
            continue

        # Horizontal rule → divider
        if tok.type == "hr":
            blocks.append({"type": "divider"})
            i += 1
            continue

        # Fenced code block
        if tok.type == "fence":
            lang = (tok.info or "").strip()
            if lang == "embed-html":
                blocks.append({"type": "embed-html", "html": tok.content})
            else:
                blocks.append({
                    "type": "code",
                    "language": lang or "text",
                    "text": tok.content.rstrip("\n"),
                })
            i += 1
            continue

        # Bulleted / numbered / todo list
        if tok.type in ("bullet_list_open", "ordered_list_open"):
            style = "bulleted" if tok.type == "bullet_list_open" else "numbered"
            i += 1
            while i < len(tokens) and tokens[i].type != f"{tok.type[:-5]}_close":
                if tokens[i].type == "list_item_open":
                    item_inline = _find_first_inline_after(tokens, i)
                    item_text = item_inline.content if item_inline else ""
                    item_children = item_inline.children if item_inline else None
                    item_block = _maybe_todo_block(item_text, item_children, style)
                    if item_block is None:
                        item_block = {
                            "type": "list",
                            "style": style,
                            "text": _walk_inline_children(item_children or [])
                                    or item_text,
                        }
                    blocks.append(item_block)
                i += 1
            i += 1  # consume closing token
            continue

        # Paragraph — the most complex path because it may carry:
        #   - URL embeds (empty-label `[](url)` alone)
        #   - Cross-doc refs `[[Doc Title]]`
        #   - Keyframe image refs `![alt](kf:N)` alone
        #   - Callout placeholders (pre-extracted earlier)
        #   - Regular rich text (bold/italic/code/links)
        if tok.type == "paragraph_open":
            inline = tokens[i + 1]
            text = inline.content
            children = inline.children or []

            # Callout placeholder?
            cb = _try_callout_placeholder(text, callout_bodies)
            if cb is not None:
                blocks.append(cb)
                i += 3
                continue

            # Standalone URL embed (paragraph contains only `[](url)`)
            embed = _try_url_embed(text)
            if embed is not None:
                blocks.append(embed)
                i += 3
                continue

            # Standalone keyframe image ref (`![alt](kf:N)` alone)
            kf_image = _try_keyframe_image(text, keyframes)
            if kf_image is not None:
                blocks.append(kf_image)
                i += 3
                continue

            # Cross-doc refs split the paragraph into multiple blocks.
            split_blocks = await _split_on_cross_doc_refs(
                text, children, mcp_client,
            )
            blocks.extend(split_blocks)
            i += 3  # paragraph_open, inline, paragraph_close
            continue

        # Stray inline outside paragraph_open — skip.
        if tok.type == "inline":
            i += 1
            continue

        # Quote block
        if tok.type == "blockquote_open":
            close_idx = _find_matching_close(
                tokens, i, "blockquote_open", "blockquote_close",
            )
            # Concatenate inline children from all paragraphs inside the quote.
            all_children: list[Token] = []
            for t in tokens[i + 1:close_idx]:
                if t.type == "inline" and t.children:
                    if all_children:
                        all_children.append(_make_text_token(" "))
                    all_children.extend(t.children)
            text_ops = _walk_inline_children(all_children) if all_children else ""
            inner_text = " ".join(
                t.content for t in tokens[i + 1:close_idx] if t.type == "inline"
            )
            blocks.append({
                "type": "paragraph",
                "style": "quote",
                "text": text_ops or inner_text,
            })
            i = close_idx + 1
            continue

        i += 1

    # Drop any blocks marked _drop (empty callouts, missing keyframes, etc.)
    # Also defensively drop any callout that ended up with empty text — they
    # render as confusing icon-only bars in AFFiNE.
    return [
        b for b in blocks
        if not b.get("_drop") and not _is_empty_callout(b)
    ]


def _is_empty_callout(block: dict[str, Any]) -> bool:
    """A callout block with no meaningful text — should not be emitted."""
    if block.get("type") != "callout":
        return False
    text = block.get("text")
    if text is None:
        return True
    if isinstance(text, str):
        return not text.strip()
    if isinstance(text, list):
        # InlineOp[] — empty if no op has any non-whitespace text.
        return not any(
            isinstance(op, dict) and (op.get("text") or "").strip()
            for op in text
        )
    return False


# ── Callout extraction ──────────────────────────────────────────────


def _extract_callouts(md: str) -> tuple[str, list[str]]:
    """Pre-extract `> [!callout] ...` blocks (single or multi-line) into
    placeholder sentinels, returning (modified_md, list_of_bodies).

    Single-line: `> [!callout] body text`
    Multi-line:
      > [!callout]
      > body line 1
      > body line 2
    The body is the head (after `[!callout]`) joined with continuation
    lines (with leading `> ` stripped), then stripped of surrounding
    whitespace.

    Empty bodies are still extracted but rendered as a no-op by the
    post-pass (we drop the callout instead of emitting an empty one,
    which the AFFiNE renderer otherwise shows as a confusing icon-only
    block).
    """
    callouts: list[str] = []

    def _replace(m: re.Match) -> str:
        head = (m.group("head") or "").strip()
        cont = m.group("cont") or ""
        body_lines: list[str] = []
        if head:
            body_lines.append(head)
        for raw in cont.split("\n"):
            stripped = raw.strip()
            if not stripped.startswith(">"):
                continue
            # Strip leading `>` plus optional space.
            content = stripped[1:].lstrip()
            body_lines.append(content)
        body = "\n".join(body_lines).strip()
        idx = len(callouts)
        callouts.append(body)
        return f"\n\n{_CALLOUT_PLACEHOLDER_PREFIX}{idx}\n\n"

    new_md = _CALLOUT_BLOCK_RE.sub(_replace, md)
    return new_md, callouts


def _try_callout_placeholder(
    text: str, callout_bodies: list[str],
) -> dict[str, Any] | None:
    """If the paragraph is exactly a callout placeholder, return the
    callout block (or None if the body is empty — we drop empty callouts
    rather than emitting an icon-only confusing block)."""
    stripped = text.strip()
    if not stripped.startswith(_CALLOUT_PLACEHOLDER_PREFIX):
        return None
    suffix = stripped[len(_CALLOUT_PLACEHOLDER_PREFIX):]
    if not suffix.isdigit():
        return None
    idx = int(suffix)
    if idx < 0 or idx >= len(callout_bodies):
        return None
    body = callout_bodies[idx].strip()
    if not body:
        # Empty-body callouts are visual noise — drop instead of emitting.
        return {"_drop": True}
    return {"type": "callout", "text": _inline_text_to_ops(body)}


# ── Inline rich-text walking (markdown-it AST → AFFiNE InlineOp[]) ──


def _walk_inline_children(children: list[Token]):
    """Walk markdown-it inline children → AFFiNE InlineOp[].

    Recognizes:
      - text                  → {text}
      - code_inline           → {text, code: true}
      - strong_open/close     → {bold: true} on enclosed text
      - em_open/close         → {italic: true} on enclosed text
      - s_open/close          → {strike: true} on enclosed text
      - link_open/close       → {link: url} on enclosed text
      - softbreak / hardbreak → space / newline
      - image (kf:N)          → handled at the block level (caller
                                detects standalone `![](kf:N)` paragraphs);
                                inline kf:N refs are dropped here with a warn

    Returns:
      - empty string if children produce no usable text
      - a plain string if no formatting attributes were applied
      - a list of InlineOp dicts otherwise
    """
    if not children:
        return ""
    ops: list[dict[str, Any]] = []
    stack = {"bold": 0, "italic": 0, "code": 0, "strike": 0}
    link_stack: list[str] = []
    any_attr = False

    def _attrs() -> dict[str, Any]:
        a: dict[str, Any] = {}
        if stack["bold"] > 0:
            a["bold"] = True
        if stack["italic"] > 0:
            a["italic"] = True
        if stack["strike"] > 0:
            a["strike"] = True
        if link_stack:
            a["link"] = link_stack[-1]
        return a

    for child in children:
        t = child.type
        if t == "text":
            if not child.content:
                continue
            attrs = _attrs()
            op: dict[str, Any] = {"text": child.content, **attrs}
            if attrs:
                any_attr = True
            ops.append(op)
        elif t == "code_inline":
            attrs = _attrs()
            attrs["code"] = True
            any_attr = True
            ops.append({"text": child.content, **attrs})
        elif t == "strong_open":
            stack["bold"] += 1
        elif t == "strong_close":
            stack["bold"] = max(0, stack["bold"] - 1)
        elif t == "em_open":
            stack["italic"] += 1
        elif t == "em_close":
            stack["italic"] = max(0, stack["italic"] - 1)
        elif t == "s_open":
            stack["strike"] += 1
        elif t == "s_close":
            stack["strike"] = max(0, stack["strike"] - 1)
        elif t == "link_open":
            href = child.attrGet("href") or ""
            link_stack.append(href)
        elif t == "link_close":
            if link_stack:
                link_stack.pop()
        elif t == "softbreak":
            ops.append({"text": " "})
        elif t == "hardbreak":
            ops.append({"text": "\n"})
        elif t == "image":
            # Inline images are out of scope; the block-level handler picks
            # up standalone `![alt](kf:N)` paragraphs separately.
            log.debug("inline image in rich text dropped: %s",
                      child.attrGet("src"))
        # else: html_inline and other oddities are ignored

    if not ops:
        return ""

    # If all ops are plain (no attributes), coalesce into one string.
    if not any_attr and not link_stack and all(
        set(op.keys()) == {"text"} for op in ops
    ):
        joined = "".join(op["text"] for op in ops)
        return joined

    return ops


def _inline_text_to_ops(text: str):
    """Parse a plain string of markdown-flavoured text into InlineOp[].

    Used for callout bodies and other text we pre-extracted before
    markdown-it parsing (so it never went through the normal pipeline).
    """
    if not text:
        return ""
    parsed = _INLINE_PARSER.parseInline(text)
    if not parsed or not parsed[0].children:
        return text
    result = _walk_inline_children(parsed[0].children)
    return result if result else text


def _make_text_token(content: str) -> Token:
    """Synthesize a markdown-it text Token for joining quote paragraphs."""
    t = Token("text", "", 0)
    t.content = content
    return t


# ── Todo list detection ─────────────────────────────────────────────


def _maybe_todo_block(
    item_text: str,
    item_children: list[Token] | None,
    parent_style: str,
) -> dict[str, Any] | None:
    """Detect GFM task-list items `[ ]` / `[x]` at the start of a list item."""
    t = item_text.lstrip()
    if t.startswith("[ ] "):
        rest_text = t[4:]
        rest_ops = _inline_text_to_ops(rest_text)
        return {"type": "list", "style": "todo", "checked": False,
                "text": rest_ops}
    if t.startswith("[x] ") or t.startswith("[X] "):
        rest_text = t[4:]
        rest_ops = _inline_text_to_ops(rest_text)
        return {"type": "list", "style": "todo", "checked": True,
                "text": rest_ops}
    return None


# ── Token-walking helpers ───────────────────────────────────────────


def _find_first_inline_after(tokens: list[Token], start: int) -> Token | None:
    for t in tokens[start + 1:]:
        if t.type == "inline":
            return t
        if t.type == "list_item_close":
            break
    return None


def _find_matching_close(
    tokens: list[Token], start: int, open_type: str, close_type: str,
) -> int:
    depth = 1
    i = start + 1
    while i < len(tokens):
        if tokens[i].type == open_type:
            depth += 1
        elif tokens[i].type == close_type:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(tokens) - 1


# ── URL embed / keyframe ref detection ──────────────────────────────


def _try_url_embed(text: str) -> dict[str, Any] | None:
    """A paragraph that contains ONLY `[](url)` becomes a URL embed."""
    m = _INLINE_LINK_RE.fullmatch(text.strip())
    if m is None or m.group("label"):
        return None
    url = m.group("url")
    host = (urlparse(url).hostname or "").lower()
    if host in ("youtu.be",) or host == "youtube.com" or host.endswith(".youtube.com"):
        return {"type": "embed-youtube", "url": url}
    if host == "github.com" or host.endswith(".github.com"):
        return {"type": "embed-github", "url": url}
    if host == "figma.com" or host.endswith(".figma.com"):
        return {"type": "embed-figma", "url": url}
    if host == "loom.com" or host.endswith(".loom.com"):
        return {"type": "embed-loom", "url": url}
    return {"type": "bookmark", "url": url}


def _try_keyframe_image(
    text: str, keyframes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """A paragraph that is exactly `![alt](kf:N)` becomes an image block
    backed by the n-th keyframe's blob_source_id. Out-of-range or non-`kf:`
    image refs return None (caller falls through to normal rendering)."""
    m = _IMAGE_REF_RE.match(text.strip())
    if m is None:
        return None
    src = m.group("src")
    kfm = _KF_REF_RE.match(src)
    if kfm is None:
        log.warning("external image ref dropped: %s", src)
        return {"_drop": True}  # consume but don't emit
    idx = int(kfm.group(1))
    if idx < 0 or idx >= len(keyframes):
        log.warning("keyframe ref kf:%d out of range (0..%d)",
                    idx, len(keyframes) - 1)
        return {"_drop": True}
    kf = keyframes[idx]
    sid = kf.get("blob_source_id")
    if not sid:
        log.warning("keyframe kf:%d missing blob_source_id", idx)
        return {"_drop": True}
    return {
        "type": "image",
        "sourceId": sid,
        "caption": m.group("alt") or kf.get("caption", ""),
    }


# ── Cross-doc refs ──────────────────────────────────────────────────


async def _split_on_cross_doc_refs(
    text: str,
    children: list[Token],
    mcp_client: Any | None,
) -> list[dict[str, Any]]:
    """Split a paragraph on `[[Doc Title]]` refs. Each ref becomes its own
    embed-linked-doc block; the surrounding text becomes adjacent paragraphs
    rendered with full inline formatting (bold/italic/code/links).
    Unresolved refs stay inline as literal text."""
    matches = list(_CROSS_DOC_RE.finditer(text))
    if not matches:
        ops = _walk_inline_children(children) if children else text
        return [{"type": "paragraph", "style": "text", "text": ops}]

    out: list[dict[str, Any]] = []
    pos = 0
    for m in matches:
        if m.start() > pos:
            pre = text[pos:m.start()]
            ops = _inline_text_to_ops(pre)
            if ops:
                out.append({"type": "paragraph", "style": "text", "text": ops})

        title = m.group(1)
        doc_id = None
        if mcp_client is not None:
            try:
                resp = await mcp_client.find_doc_by_title(title)
                matches_resp = resp.get("matches") if isinstance(resp, dict) else None
                if matches_resp and len(matches_resp) == 1:
                    doc_id = matches_resp[0].get("id")
            except Exception as e:  # noqa: BLE001
                log.warning("find_doc_by_title failed for %r: %s", title, e)

        if doc_id is not None:
            out.append({"type": "embed-linked-doc", "docId": doc_id})
        else:
            out.append({"type": "paragraph", "style": "text",
                        "text": f"[[{title}]]"})

        pos = m.end()
    if pos < len(text):
        tail = text[pos:]
        ops = _inline_text_to_ops(tail)
        if ops:
            out.append({"type": "paragraph", "style": "text", "text": ops})
    return [b for b in out if not b.get("_drop")]
