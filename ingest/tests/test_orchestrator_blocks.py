"""Pure-unit tests for the orchestrator's markdown→blocks helpers.

These functions don't touch MCP or Anthropic; we test the layout logic
in isolation. The full orchestrator flow is tested via test_orchestrator.py.
"""

from src.pipeline.extracted import Extracted, MediaKind
from src.pipeline.orchestrator import _build_body_blocks, _markdown_to_blocks


def _e(body_md: str, *, description: str | None = None, title: str | None = None) -> Extracted:
    extra: dict = {}
    if description is not None:
        extra["description"] = description
    return Extracted(
        title=title,
        body_md=body_md,
        author=None,
        published_at=None,
        media_kind=MediaKind.VIDEO,
        extra=extra,
    )


# ── _markdown_to_blocks ─────────────────────────────────────────────


def test_markdown_to_blocks_splits_h2_heading():
    md = "Intro paragraph.\n\n## Transcript\n\nLine A\nLine B"
    blocks = _markdown_to_blocks(md)
    assert blocks == [
        {"type": "paragraph", "style": "text", "text": "Intro paragraph."},
        {"type": "paragraph", "style": "h2", "text": "Transcript"},
        {"type": "paragraph", "style": "text", "text": "Line A\nLine B"},
    ]


def test_markdown_to_blocks_parses_inline_link_to_inline_ops():
    """Phase 12.5 fix #10: paragraph text containing `[label](url)` markdown
    must be emitted as InlineOp[] with a `link` attribute, otherwise AFFiNE
    renders the literal `[label](url)` syntax (which is what was happening
    in the user's transcript timestamp output)."""
    md = "[**0:42**](https://youtube.com/watch?v=abc&t=42s) Hello world."
    blocks = _markdown_to_blocks(md)
    assert len(blocks) == 1
    block = blocks[0]
    assert block["type"] == "paragraph"
    assert block["style"] == "text"
    # Text is now InlineOp list, not a plain string
    text = block["text"]
    assert isinstance(text, list)
    assert text[0] == {
        "text": "0:42",
        "link": "https://youtube.com/watch?v=abc&t=42s",
        "bold": True,
    }
    assert text[1]["text"] == " Hello world."


def test_markdown_to_blocks_plain_text_unchanged():
    """No inline links → fast path keeps plain string (no parser overhead)."""
    md = "Just a plain paragraph with no links."
    blocks = _markdown_to_blocks(md)
    assert blocks[0]["text"] == "Just a plain paragraph with no links."


def test_markdown_to_blocks_multiple_inline_links():
    """Multiple links in one paragraph all get converted."""
    md = "First [foo](https://foo.com) middle [bar](https://bar.com) end."
    blocks = _markdown_to_blocks(md)
    text = blocks[0]["text"]
    assert isinstance(text, list)
    # Sequence: "First " + foo-link + " middle " + bar-link + " end."
    assert any(op.get("link") == "https://foo.com" for op in text)
    assert any(op.get("link") == "https://bar.com" for op in text)
    assert text[0]["text"] == "First "


def test_markdown_to_blocks_handles_h1_to_h6():
    md = "# H1\n\n## H2\n\n### H3\n\n#### H4\n\n##### H5\n\n###### H6"
    blocks = _markdown_to_blocks(md)
    styles = [b["style"] for b in blocks]
    assert styles == ["h1", "h2", "h3", "h4", "h5", "h6"]


def test_markdown_to_blocks_blank_lines_separate_paragraphs():
    md = "Para one.\n\nPara two.\n\nPara three."
    blocks = _markdown_to_blocks(md)
    assert [b["text"] for b in blocks] == ["Para one.", "Para two.", "Para three."]


def test_markdown_to_blocks_skip_top_metadata_drops_cobalt_preamble():
    md = "**My Title**\n\n_by Travis Scott_\n\nSource: https://example.com/x\n\n## Transcript\n\nbody"
    blocks = _markdown_to_blocks(md, skip_top_metadata=True)
    # Should drop the bold/italic/Source preamble, keep just the heading + body.
    assert blocks == [
        {"type": "paragraph", "style": "h2", "text": "Transcript"},
        {"type": "paragraph", "style": "text", "text": "body"},
    ]


def test_markdown_to_blocks_empty_input():
    assert _markdown_to_blocks("") == []
    assert _markdown_to_blocks("   \n\n   ") == []


# ── _build_body_blocks ──────────────────────────────────────────────


def test_build_body_blocks_with_summary_and_description():
    blocks = _build_body_blocks(
        extracted=_e(
            "## Transcript (Whisper via cobalt)\n\nactual transcript",
            description="The video creator described it as a deep dive into X.",
        ),
        summary_md="A 2-sentence AI-generated summary of the content.",
        url="https://www.instagram.com/reel/abc/",
    )

    headings = [b["text"] for b in blocks if b.get("style", "").startswith("h")]
    assert headings == ["Summary", "Description", "Transcript (Whisper via cobalt)"]

    # Last block must be the Source: link
    assert blocks[-1]["style"] == "text"
    inline = blocks[-1]["text"]
    assert isinstance(inline, list)
    assert inline[0]["text"] == "Source: "
    assert inline[1]["text"] == "https://www.instagram.com/reel/abc/"
    assert inline[1].get("link") == "https://www.instagram.com/reel/abc/"


def test_build_body_blocks_without_summary_keeps_existing_layout():
    blocks = _build_body_blocks(
        extracted=_e("## Transcript\n\nbody text"),
        summary_md=None,
        url=None,
    )
    assert blocks[0] == {"type": "paragraph", "style": "h2", "text": "Transcript"}
    assert blocks[1] == {"type": "paragraph", "style": "text", "text": "body text"}


def test_build_body_blocks_empty_inputs_returns_placeholder():
    blocks = _build_body_blocks(
        extracted=_e(""),
        summary_md=None,
        url=None,
    )
    assert blocks == [{"type": "paragraph", "style": "text", "text": "(no extracted content)"}]


def test_build_body_blocks_drops_cobalt_metadata_preamble_when_summary_present():
    """When we have a Summary block, we don't want the bold-title / by-author preamble repeated."""
    md = "**My Title**\n\n_by Channel_\n\nSource: https://example.com/x\n\n## Transcript\n\nbody"
    blocks = _build_body_blocks(
        extracted=_e(md),
        summary_md="An AI summary.",
        url="https://example.com/x",
    )
    # The bold/by/source lines should not appear as their own paragraphs.
    paragraph_texts = [b["text"] for b in blocks if b["style"] == "text" and isinstance(b["text"], str)]
    assert all("_by Channel_" not in t for t in paragraph_texts)
    assert all(not t.startswith("**My Title**") for t in paragraph_texts)


# ── Phase 13: keyframe → image block emission ──────────────────────


def _e_with_keyframes(keyframes: list[dict]) -> Extracted:
    return Extracted(
        title="A Video",
        body_md="## Transcript\n\nfull transcript here",
        author=None,
        published_at=None,
        media_kind=MediaKind.VIDEO,
        extra={"keyframes": keyframes},
    )


def test_build_body_blocks_emits_image_blocks_for_keyframes():
    """When extracted.extra.keyframes is non-empty, emit a `## Keyframes`
    heading + one image block + caption paragraph per keyframe."""
    keyframes = [
        {"blob_source_id": "blob-abc", "caption": "Title screen", "timestamp_seconds": 0.5},
        {"blob_source_id": "blob-def", "caption": "Code snippet", "timestamp_seconds": 12.3},
    ]
    blocks = _build_body_blocks(
        extracted=_e_with_keyframes(keyframes),
        summary_md="grounded summary",
        url="https://example.com/v",
    )

    # Find the Keyframes h2
    h2s = [b for b in blocks if b.get("style") == "h2"]
    h2_texts = [b["text"] for b in h2s]
    assert "Keyframes" in h2_texts

    # Image blocks must be present with our sourceIds
    image_blocks = [b for b in blocks if b.get("type") == "image"]
    assert len(image_blocks) == 2
    assert image_blocks[0]["sourceId"] == "blob-abc"
    assert image_blocks[0]["caption"] == "Title screen"
    assert image_blocks[1]["sourceId"] == "blob-def"
    assert image_blocks[1]["caption"] == "Code snippet"


def test_build_body_blocks_no_keyframes_no_keyframes_heading():
    """Empty keyframes list = no Keyframes section emitted at all."""
    blocks = _build_body_blocks(
        extracted=_e_with_keyframes([]),
        summary_md="just text",
        url=None,
    )
    h2_texts = [b["text"] for b in blocks if b.get("style") == "h2"]
    assert "Keyframes" not in h2_texts
    assert not any(b.get("type") == "image" for b in blocks)


def test_build_body_blocks_skips_keyframes_with_missing_source_id():
    """Defensive: keyframes without blob_source_id are dropped (not crash)."""
    keyframes = [
        {"blob_source_id": "blob-ok", "caption": "ok", "timestamp_seconds": 1.0},
        {"caption": "no source — should be dropped", "timestamp_seconds": 2.0},
        {"blob_source_id": "", "caption": "empty source — also dropped", "timestamp_seconds": 3.0},
    ]
    blocks = _build_body_blocks(
        extracted=_e_with_keyframes(keyframes),
        summary_md=None,
        url=None,
    )
    image_blocks = [b for b in blocks if b.get("type") == "image"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["sourceId"] == "blob-ok"
