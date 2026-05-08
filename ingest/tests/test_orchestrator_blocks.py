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
