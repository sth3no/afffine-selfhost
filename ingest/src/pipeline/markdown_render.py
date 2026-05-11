"""Markdown → AFFiNE block-spec emitter.

Uses `markdown-it-py` for CommonMark parsing. Adds project-specific
syntax handled inline:
  - Fenced code blocks with language sentinel `embed-html` → embed-html block
  - Image refs `![alt](kf:<n>)` → image block backed by keyframe blob_source_id
  - Cross-doc refs `[[Doc Title]]` → embed-linked-doc block (async MCP call)
  - Callout blocks `> [!callout] text` → affine:callout block

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
_CALLOUT_RE = re.compile(r"^\s*>\s*\[!callout\]\s*(.*)$", re.MULTILINE)
_CROSS_DOC_RE = re.compile(r"\[\[([^\]]+)\]\]")
_INLINE_LINK_RE = re.compile(
    r"\[(?P<label>[^\]]*)\]\((?P<url>[^)\s]+)\)"
)


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
    # Pre-pass: convert callout lines to a sentinel that the token stream can pick up.
    md = _CALLOUT_RE.sub(r":::callout\n\1\n:::", md)

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
                "text": _inline_to_text(inline.content),
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
                    item_block = _maybe_todo_block(item_text, style)
                    if item_block is None:
                        item_block = {
                            "type": "list",
                            "style": style,
                            "text": _inline_to_text(item_text),
                        }
                    blocks.append(item_block)
                i += 1
            i += 1  # consume closing token
            continue

        # Paragraph (default)
        if tok.type == "paragraph_open":
            inline = tokens[i + 1]
            text = inline.content
            embed = _try_url_embed(text)
            if embed is not None:
                blocks.append(embed)
                i += 3
                continue
            new_blocks = await _split_on_cross_doc_refs(text, mcp_client)
            new_blocks = _replace_keyframe_refs(new_blocks, keyframes)
            blocks.extend(new_blocks)
            i += 3  # paragraph_open, inline, paragraph_close
            continue

        # Stray inline outside paragraph_open — skip.
        if tok.type == "inline":
            i += 1
            continue

        # Quote block (not callout — already pre-transformed)
        if tok.type == "blockquote_open":
            close_idx = _find_matching_close(tokens, i, "blockquote_open", "blockquote_close")
            inner_text = " ".join(
                t.content for t in tokens[i + 1:close_idx] if t.type == "inline"
            )
            blocks.append({
                "type": "paragraph",
                "style": "quote",
                "text": _inline_to_text(inner_text),
            })
            i = close_idx + 1
            continue

        i += 1

    return _convert_callout_pseudoblocks(blocks)


def _convert_callout_pseudoblocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for b in blocks:
        text = b.get("text")
        flat = text if isinstance(text, str) else (
            " ".join(op.get("text", "") for op in text) if isinstance(text, list) else ""
        )
        if b.get("type") == "paragraph" and flat.startswith(":::callout"):
            body = flat[len(":::callout"):].strip().rstrip(":").strip()
            out.append({"type": "callout", "text": body})
            continue
        out.append(b)
    return out


def _maybe_todo_block(item_text: str, parent_style: str) -> dict[str, Any] | None:
    """Detect GFM task-list items `[ ]` / `[x]` at the start of a list item."""
    t = item_text.lstrip()
    if t.startswith("[ ] "):
        return {"type": "list", "style": "todo", "checked": False, "text": t[4:]}
    if t.startswith("[x] ") or t.startswith("[X] "):
        return {"type": "list", "style": "todo", "checked": True, "text": t[4:]}
    return None


def _find_first_inline_after(tokens: list[Token], start: int) -> Token | None:
    for t in tokens[start + 1:]:
        if t.type == "inline":
            return t
        if t.type == "list_item_close":
            break
    return None


def _find_matching_close(
    tokens: list[Token], start: int, open_type: str, close_type: str
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


def _inline_to_text(text: str):
    """Parse `[label](url)` inline links into the inline-op list shape
    that mcp-ext's block-builder converts to rich-text deltas."""
    if "](" not in text:
        return text
    parts: list[dict[str, Any]] = []
    pos = 0
    for m in _INLINE_LINK_RE.finditer(text):
        if m.start() > pos:
            parts.append({"text": text[pos:m.start()]})
        label = m.group("label") or m.group("url")
        parts.append({"text": label, "link": m.group("url")})
        pos = m.end()
    if pos < len(text):
        parts.append({"text": text[pos:]})
    return parts if parts else text


async def _split_on_cross_doc_refs(
    text: str, mcp_client: Any | None
) -> list[dict[str, Any]]:
    """Split a paragraph on `[[Doc Title]]` refs. Each ref becomes its own
    embed-linked-doc block; the surrounding text becomes adjacent paragraphs.
    Unresolved refs stay inline as literal text.

    Note: paragraph blocks are returned with raw string `text` so that
    `_replace_keyframe_refs` can inspect them for `![alt](kf:N)` image-ref
    syntax. `_inline_to_text` is applied afterwards in that function.
    """
    matches = list(_CROSS_DOC_RE.finditer(text))
    if not matches:
        return [{"type": "paragraph", "style": "text", "text": text}]

    out: list[dict[str, Any]] = []
    pos = 0
    for m in matches:
        if m.start() > pos:
            pre = text[pos:m.start()]
            out.append({"type": "paragraph", "style": "text", "text": pre})

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
        out.append({"type": "paragraph", "style": "text",
                    "text": text[pos:]})
    return out


_IMAGE_REF_RE = re.compile(r"^!\[(?P<alt>[^\]]*)\]\((?P<src>[^)\s]+)\)\s*$")


def _replace_keyframe_refs(
    blocks: list[dict[str, Any]], keyframes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Walk paragraph blocks; if a paragraph is exactly an image ref
    `![alt](kf:N)`, replace with an image block backed by the keyframe.

    For paragraphs that are NOT image refs, `_inline_to_text` is applied
    here to convert any `[label](url)` links into inline-op lists.
    This is done after image-ref detection so the raw string is available
    for `_IMAGE_REF_RE` matching.
    """
    out: list[dict[str, Any]] = []
    for b in blocks:
        if b.get("type") != "paragraph":
            out.append(b)
            continue
        flat = b.get("text")
        if not isinstance(flat, str):
            # Already processed (e.g. inline-op list) — pass through.
            out.append(b)
            continue
        m = _IMAGE_REF_RE.match(flat.strip())
        if m is None:
            # Not an image ref: apply inline-link conversion and keep as paragraph.
            out.append({**b, "text": _inline_to_text(flat)})
            continue
        src = m.group("src")
        kfm = _KF_REF_RE.match(src)
        if kfm is None:
            log.warning("external image ref dropped: %s", src)
            continue
        idx = int(kfm.group(1))
        if idx < 0 or idx >= len(keyframes):
            log.warning("keyframe ref kf:%d out of range (0..%d)", idx, len(keyframes) - 1)
            continue
        kf = keyframes[idx]
        sid = kf.get("blob_source_id")
        if not sid:
            log.warning("keyframe kf:%d missing blob_source_id", idx)
            continue
        out.append({
            "type": "image",
            "sourceId": sid,
            "caption": m.group("alt") or kf.get("caption", ""),
        })
    return out
