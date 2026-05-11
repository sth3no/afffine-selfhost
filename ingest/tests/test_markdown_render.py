"""Tests for markdown → AFFiNE block-spec emitter.

Each block flavour gets its own round-trip test. The MCP client is mocked
for [[Doc Title]] resolution; keyframes are passed as a list of dicts.
"""

from unittest.mock import AsyncMock

import pytest

from src.pipeline.markdown_render import markdown_to_blocks


KEYFRAMES = [
    {"timestamp_seconds": 42.3, "caption": "IDE", "blob_source_id": "blob1"},
    {"timestamp_seconds": 154.0, "caption": "Network", "blob_source_id": "blob2"},
]


@pytest.mark.asyncio
async def test_plain_paragraph():
    blocks = await markdown_to_blocks("Hello world.", keyframes=[], mcp_client=None)
    assert len(blocks) == 1
    assert blocks[0]["type"] == "paragraph"
    assert blocks[0]["style"] == "text"
    assert "Hello world." in str(blocks[0]["text"])


@pytest.mark.asyncio
async def test_heading_levels():
    blocks = await markdown_to_blocks(
        "# H1\n## H2\n### H3\n", keyframes=[], mcp_client=None
    )
    styles = [b["style"] for b in blocks]
    assert styles == ["h1", "h2", "h3"]


@pytest.mark.asyncio
async def test_bulleted_list():
    blocks = await markdown_to_blocks("- a\n- b\n", keyframes=[], mcp_client=None)
    assert len(blocks) == 2
    assert all(b["type"] == "list" and b["style"] == "bulleted" for b in blocks)


@pytest.mark.asyncio
async def test_numbered_list():
    blocks = await markdown_to_blocks("1. a\n2. b\n3. c\n", keyframes=[], mcp_client=None)
    assert len(blocks) == 3
    assert all(b["type"] == "list" and b["style"] == "numbered" for b in blocks)


@pytest.mark.asyncio
async def test_todo_list():
    blocks = await markdown_to_blocks(
        "- [ ] one\n- [x] two\n", keyframes=[], mcp_client=None
    )
    assert len(blocks) == 2
    assert all(b["type"] == "list" and b["style"] == "todo" for b in blocks)
    assert blocks[0].get("checked") is False
    assert blocks[1].get("checked") is True


@pytest.mark.asyncio
async def test_fenced_code_block_with_language():
    md = "```python\nprint('hi')\n```\n"
    blocks = await markdown_to_blocks(md, keyframes=[], mcp_client=None)
    assert len(blocks) == 1
    assert blocks[0]["type"] == "code"
    assert blocks[0]["language"] == "python"
    assert "print('hi')" in blocks[0]["text"]


@pytest.mark.asyncio
async def test_mermaid_renders_as_code_with_language():
    md = "```mermaid\nflowchart TD\n  A --> B\n```\n"
    blocks = await markdown_to_blocks(md, keyframes=[], mcp_client=None)
    assert blocks[0]["type"] == "code"
    assert blocks[0]["language"] == "mermaid"


@pytest.mark.asyncio
async def test_embed_html_sentinel():
    md = "```embed-html\n<svg width='10'/>\n```\n"
    blocks = await markdown_to_blocks(md, keyframes=[], mcp_client=None)
    assert blocks[0]["type"] == "embed-html"
    assert "<svg" in blocks[0]["html"]


@pytest.mark.asyncio
async def test_divider():
    blocks = await markdown_to_blocks("---\n", keyframes=[], mcp_client=None)
    assert blocks[0]["type"] == "divider"


@pytest.mark.asyncio
async def test_callout_syntax():
    blocks = await markdown_to_blocks(
        "> [!callout] Important point.", keyframes=[], mcp_client=None
    )
    assert blocks[0]["type"] == "callout"
    assert "Important point" in str(blocks[0]["text"])


@pytest.mark.asyncio
async def test_keyframe_image_ref_resolves_to_blob_id():
    md = "![the IDE](kf:0)\n"
    blocks = await markdown_to_blocks(md, keyframes=KEYFRAMES, mcp_client=None)
    assert blocks[0]["type"] == "image"
    assert blocks[0]["sourceId"] == "blob1"
    assert blocks[0].get("caption") == "the IDE"


@pytest.mark.asyncio
async def test_keyframe_out_of_range_dropped_silently():
    md = "![nope](kf:99)\nNext paragraph.\n"
    blocks = await markdown_to_blocks(md, keyframes=KEYFRAMES, mcp_client=None)
    # The image block is dropped; the paragraph after it survives.
    assert all(b["type"] != "image" for b in blocks)
    assert any("Next paragraph." in str(b.get("text", "")) for b in blocks)


@pytest.mark.asyncio
async def test_cross_doc_reference_with_match():
    mcp = AsyncMock()
    mcp.find_doc_by_title = AsyncMock(return_value={"matches": [{"id": "doc_abc"}]})
    blocks = await markdown_to_blocks(
        "See [[Phase 13 Plan]] for context.\n",
        keyframes=[],
        mcp_client=mcp,
    )
    # Cross-doc embed appears as its own block; the surrounding text
    # may break into preceding/following paragraphs.
    embeds = [b for b in blocks if b["type"] == "embed-linked-doc"]
    assert len(embeds) == 1
    assert embeds[0]["docId"] == "doc_abc"


@pytest.mark.asyncio
async def test_cross_doc_reference_unresolved_falls_back_to_text():
    mcp = AsyncMock()
    mcp.find_doc_by_title = AsyncMock(return_value={"matches": []})
    blocks = await markdown_to_blocks(
        "See [[Nonexistent Doc]] for context.\n",
        keyframes=[],
        mcp_client=mcp,
    )
    # No embed-linked-doc; the literal text remains.
    assert all(b["type"] != "embed-linked-doc" for b in blocks)
    flat = " ".join(str(b.get("text", "")) for b in blocks)
    assert "[[Nonexistent Doc]]" in flat


@pytest.mark.asyncio
async def test_inline_link_in_paragraph():
    blocks = await markdown_to_blocks(
        "Read [the paper](https://example.com/paper.pdf) now.\n",
        keyframes=[],
        mcp_client=None,
    )
    assert blocks[0]["type"] == "paragraph"
    # text becomes a list of inline ops when there's any rich formatting.
    ops = blocks[0]["text"]
    assert isinstance(ops, list)
    linked = [op for op in ops if isinstance(op, dict) and op.get("link")]
    assert len(linked) == 1
    assert linked[0]["link"] == "https://example.com/paper.pdf"


@pytest.mark.asyncio
async def test_url_embed_with_empty_label_promotes_to_embed():
    """`[](https://www.youtube.com/watch?v=X)` (no label) → embed-youtube block."""
    blocks = await markdown_to_blocks(
        "[](https://www.youtube.com/watch?v=dQw4w9WgXcQ)\n",
        keyframes=[],
        mcp_client=None,
    )
    assert blocks[0]["type"] == "embed-youtube"


@pytest.mark.asyncio
async def test_todo_item_with_inline_link_preserved():
    md = "- [ ] Read [the paper](https://example.com/paper.pdf) tonight\n"
    blocks = await markdown_to_blocks(md, keyframes=[], mcp_client=None)
    assert len(blocks) == 1
    assert blocks[0]["type"] == "list"
    assert blocks[0]["style"] == "todo"
    assert blocks[0]["checked"] is False
    # text should be an inline-op list with the link preserved
    ops = blocks[0]["text"]
    assert isinstance(ops, list)
    linked = [op for op in ops if isinstance(op, dict) and op.get("link")]
    assert len(linked) == 1
    assert linked[0]["link"] == "https://example.com/paper.pdf"


@pytest.mark.asyncio
async def test_blockquote_renders_as_quote_paragraph():
    blocks = await markdown_to_blocks(
        "> A quoted sentence.\n", keyframes=[], mcp_client=None
    )
    assert len(blocks) == 1
    assert blocks[0]["type"] == "paragraph"
    assert blocks[0]["style"] == "quote"
    assert "quoted sentence" in str(blocks[0]["text"])


# ── Inline rich text (Phase 14.3 fix) ────────────────────────────────


@pytest.mark.asyncio
async def test_bold_renders_as_inline_op_not_literal_asterisks():
    """Pre-fix: `**bold**` rendered with literal `**` in AFFiNE. Post-fix:
    becomes an InlineOp with `bold: true`."""
    blocks = await markdown_to_blocks(
        "Hello **important word** there.", keyframes=[], mcp_client=None,
    )
    assert len(blocks) == 1
    ops = blocks[0]["text"]
    assert isinstance(ops, list), "rich text must produce inline-op list, not bare string"
    bold_ops = [op for op in ops if isinstance(op, dict) and op.get("bold")]
    assert len(bold_ops) == 1
    assert bold_ops[0]["text"] == "important word"
    # No literal asterisks survive anywhere.
    flat = "".join(op.get("text", "") for op in ops if isinstance(op, dict))
    assert "**" not in flat


@pytest.mark.asyncio
async def test_italic_underscore_renders_as_inline_op():
    blocks = await markdown_to_blocks(
        "Wrote _by Author Name_ in the byline.", keyframes=[], mcp_client=None,
    )
    ops = blocks[0]["text"]
    assert isinstance(ops, list)
    italic_ops = [op for op in ops if isinstance(op, dict) and op.get("italic")]
    assert len(italic_ops) == 1
    assert italic_ops[0]["text"] == "by Author Name"
    flat = "".join(op.get("text", "") for op in ops if isinstance(op, dict))
    assert "_by" not in flat  # underscores should be stripped


@pytest.mark.asyncio
async def test_inline_code_renders_with_code_attribute():
    blocks = await markdown_to_blocks(
        "Use `border-image` for the panels.", keyframes=[], mcp_client=None,
    )
    ops = blocks[0]["text"]
    assert isinstance(ops, list)
    code_ops = [op for op in ops if isinstance(op, dict) and op.get("code")]
    assert len(code_ops) == 1
    assert code_ops[0]["text"] == "border-image"
    flat = "".join(op.get("text", "") for op in ops if isinstance(op, dict))
    assert "`" not in flat


@pytest.mark.asyncio
async def test_combined_bold_and_code_in_list_item():
    """Bullet `**ChatGPT Image 2 (GPT-4o image generation)** — preferred...`
    should produce bold-attributed text on the term, plain text on the rest.
    This is the exact case the user reported."""
    blocks = await markdown_to_blocks(
        "- **ChatGPT Image 2 (GPT-4o image generation)** — preferred for "
        "iterative UI component work\n",
        keyframes=[], mcp_client=None,
    )
    assert len(blocks) == 1
    assert blocks[0]["type"] == "list"
    ops = blocks[0]["text"]
    assert isinstance(ops, list)
    bold_ops = [op for op in ops if isinstance(op, dict) and op.get("bold")]
    assert len(bold_ops) == 1
    assert "ChatGPT Image 2" in bold_ops[0]["text"]
    flat = "".join(op.get("text", "") for op in ops if isinstance(op, dict))
    assert "**" not in flat


@pytest.mark.asyncio
async def test_bold_in_heading():
    blocks = await markdown_to_blocks(
        "## **Important** topic\n", keyframes=[], mcp_client=None,
    )
    assert blocks[0]["style"] == "h2"
    ops = blocks[0]["text"]
    assert isinstance(ops, list)
    bold_ops = [op for op in ops if isinstance(op, dict) and op.get("bold")]
    assert len(bold_ops) == 1
    assert bold_ops[0]["text"] == "Important"


@pytest.mark.asyncio
async def test_link_in_paragraph_uses_walker_not_regex():
    """The new walker handles links via markdown-it; this test confirms
    the link attribute survives the inline-op path."""
    blocks = await markdown_to_blocks(
        "See [the paper](https://example.com/paper.pdf) for context.",
        keyframes=[], mcp_client=None,
    )
    ops = blocks[0]["text"]
    assert isinstance(ops, list)
    linked = [op for op in ops if isinstance(op, dict) and op.get("link")]
    assert len(linked) == 1
    assert linked[0]["link"] == "https://example.com/paper.pdf"
    assert linked[0]["text"] == "the paper"


# ── Callout edge cases (Phase 14.3 fix) ──────────────────────────────


@pytest.mark.asyncio
async def test_empty_callout_is_dropped_not_emitted_with_empty_body():
    """Production showed empty callout boxes with a default emoji and no
    text. The renderer must drop callouts whose body strips to empty."""
    blocks = await markdown_to_blocks(
        "## A heading\n\n> [!callout]\n\nMore content.\n",
        keyframes=[], mcp_client=None,
    )
    # No callout block emitted.
    assert all(b.get("type") != "callout" for b in blocks)
    # Heading and the "More content." paragraph survive.
    assert any(b.get("style") == "h2" for b in blocks)
    flat = " ".join(
        str(b.get("text", "")) for b in blocks
    )
    assert "More content." in flat


@pytest.mark.asyncio
async def test_multi_line_callout_with_body_on_continuation_lines():
    """`> [!callout]\n> Body line 1\n> Body line 2` is parsed as one
    callout with both lines as the body."""
    md = "> [!callout]\n> First line of body.\n> Second line.\n"
    blocks = await markdown_to_blocks(md, keyframes=[], mcp_client=None)
    callouts = [b for b in blocks if b.get("type") == "callout"]
    assert len(callouts) == 1
    body = callouts[0]["text"]
    flat = body if isinstance(body, str) else "".join(
        op.get("text", "") for op in body if isinstance(op, dict)
    )
    assert "First line of body" in flat
    assert "Second line" in flat


@pytest.mark.asyncio
async def test_single_line_callout_with_inline_bold():
    """`> [!callout] **Important**: key insight` produces a callout whose
    body contains a bold inline op."""
    md = "> [!callout] **Important**: this is the key insight.\n"
    blocks = await markdown_to_blocks(md, keyframes=[], mcp_client=None)
    callouts = [b for b in blocks if b.get("type") == "callout"]
    assert len(callouts) == 1
    body = callouts[0]["text"]
    assert isinstance(body, list), "callout body with bold should be inline-op list"
    bold_ops = [op for op in body if isinstance(op, dict) and op.get("bold")]
    assert len(bold_ops) == 1
    assert bold_ops[0]["text"] == "Important"


# ── strip_extractor_metadata helper ──────────────────────────────────


def test_strip_extractor_metadata_removes_title_author_source_and_inner_heading():
    """Cobalt extractor body prefix: `**Title**`, `_by Author_`, `Source:`,
    `## Transcript (...)` — all must be stripped before the orchestrator
    appends its own `## Transcript` wrapper."""
    from src.pipeline.orchestrator import strip_extractor_metadata

    body = (
        "**Why Can't We Build UIs Like Blizzard?**\n\n"
        "_by Web Dev Cody_\n\n"
        "Source: https://www.youtube.com/watch?v=ceXRl1OaFhc\n\n"
        "## Transcript (YouTube captions)\n\n"
        "[0:00] So recently there was a trend on X...\n"
    )
    out = strip_extractor_metadata(body)
    assert out.startswith("[0:00]")
    assert "Why Can't We Build" not in out
    assert "Web Dev Cody" not in out
    assert "Source:" not in out
    assert "## Transcript" not in out


def test_strip_extractor_metadata_passthrough_when_no_prefix():
    """If body_md has no extractor prefix, strip is a no-op."""
    from src.pipeline.orchestrator import strip_extractor_metadata

    body = "Regular content without the cobalt prefix.\nMore stuff."
    assert strip_extractor_metadata(body) == body


# ── Defensive empty-callout filter (Phase 14.4) ─────────────────────


@pytest.mark.asyncio
async def test_callout_with_whitespace_only_body_is_dropped():
    """Belt-and-suspenders: the safety filter at the end of
    markdown_to_blocks drops callouts that ended up with empty/whitespace
    text. Catches stragglers from any code path that emits a callout
    without checking the text content."""
    # `> [!callout]    ` → empty body after strip → drop.
    blocks = await markdown_to_blocks(
        "## A heading\n\n> [!callout]    \n\nNext paragraph.\n",
        keyframes=[], mcp_client=None,
    )
    callouts = [b for b in blocks if b.get("type") == "callout"]
    assert len(callouts) == 0


@pytest.mark.asyncio
async def test_callout_with_only_whitespace_inline_ops_is_dropped():
    """Callout whose inline-op text is only whitespace should also be dropped."""
    # The placeholder mechanism handles this directly; this test ensures
    # the safety filter is also covering the case for any callout that
    # might slip through with InlineOp[] all-whitespace text.
    from src.pipeline.markdown_render import _is_empty_callout

    assert _is_empty_callout({"type": "callout", "text": ""}) is True
    assert _is_empty_callout({"type": "callout", "text": "   "}) is True
    assert _is_empty_callout({"type": "callout", "text": None}) is True
    assert _is_empty_callout(
        {"type": "callout", "text": [{"text": "   "}, {"text": ""}]}
    ) is True
    assert _is_empty_callout(
        {"type": "callout", "text": [{"text": "real content"}]}
    ) is False
    assert _is_empty_callout({"type": "callout", "text": "Real text"}) is False
    # Non-callout blocks pass through.
    assert _is_empty_callout({"type": "paragraph", "text": ""}) is False
