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
