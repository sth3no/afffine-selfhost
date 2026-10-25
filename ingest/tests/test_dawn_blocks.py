import pytest

from src.dawn_blocks import to_append_block_args, to_delta


def test_plain_string_text_passes_through():
    assert to_delta("hi") == "hi"


def test_inline_ops_become_delta_with_attributes():
    ops = [{"text": "a", "bold": True, "link": "https://x"}, "b", {"text": "c", "code": True}]
    assert to_delta(ops) == [
        {"insert": "a", "attributes": {"bold": True, "link": "https://x"}},
        {"insert": "b"},
        {"insert": "c", "attributes": {"code": True}},
    ]


def test_ref_doc_op_becomes_linked_page_reference():
    assert to_delta([{"text": "Recipe", "refDocId": "d9"}]) == [
        {"insert": " ", "attributes": {"reference": {"type": "LinkedPage", "pageId": "d9"}}},
    ]


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ({"type": "paragraph", "text": "hi"}, {"type": "paragraph", "text": "hi"}),
        ({"type": "paragraph", "style": "text", "text": "hi"}, {"type": "paragraph", "text": "hi"}),
        ({"type": "paragraph", "style": "h2", "text": "Summary"}, {"type": "heading", "level": 2, "text": "Summary"}),
        ({"type": "paragraph", "style": "quote", "text": "q"}, {"type": "quote", "text": "q"}),
        ({"type": "list", "style": "todo", "checked": True, "text": "x"},
         {"type": "list", "style": "todo", "checked": True, "text": "x"}),
        ({"type": "list", "style": "bulleted", "text": "x"}, {"type": "list", "style": "bulleted", "text": "x"}),
        ({"type": "code", "language": "py", "text": "x = 1"}, {"type": "code", "language": "py", "text": "x = 1"}),
        ({"type": "divider"}, {"type": "divider"}),
        ({"type": "callout", "emoji": "💡", "text": "lede"}, {"type": "callout", "text": "lede"}),
        ({"type": "latex", "latex": "e=mc^2"}, {"type": "latex", "latex": "e=mc^2"}),
        ({"type": "bookmark", "url": "https://a.b", "style": "citation", "caption": "c"},
         {"type": "bookmark", "url": "https://a.b", "bookmarkStyle": "citation", "caption": "c"}),
        ({"type": "embed-youtube", "url": "https://youtu.be/x"}, {"type": "embed_youtube", "url": "https://youtu.be/x"}),
        ({"type": "embed-github", "url": "https://github.com/a/b"}, {"type": "embed_github", "url": "https://github.com/a/b"}),
        ({"type": "embed-figma", "url": "https://figma.com/f"}, {"type": "embed_figma", "url": "https://figma.com/f"}),
        ({"type": "embed-loom", "url": "https://loom.com/v"}, {"type": "embed_loom", "url": "https://loom.com/v"}),
        ({"type": "embed-iframe", "url": "https://miro.com/x"}, {"type": "embed_iframe", "url": "https://miro.com/x"}),
        ({"type": "embed-html", "html": "<b>x</b>"}, {"type": "embed_html", "html": "<b>x</b>"}),
        ({"type": "embed-linked-doc", "docId": "d1"}, {"type": "embed_linked_doc", "pageId": "d1"}),
        ({"type": "embed-synced-doc", "docId": "d1"}, {"type": "embed_synced_doc", "pageId": "d1"}),
        ({"type": "image", "sourceId": "cap-frame-01.jpg", "caption": "kf"},
         {"type": "image", "sourceId": "cap-frame-01.jpg", "caption": "kf"}),
    ],
)
def test_block_spec_maps_to_append_block_args(spec, expected):
    assert to_append_block_args(spec) == expected


def test_rich_text_inside_block_is_converted():
    spec = {"type": "callout", "text": [{"text": "bold", "bold": True}]}
    assert to_append_block_args(spec) == {"type": "callout", "text": [{"insert": "bold", "attributes": {"bold": True}}]}


def test_unknown_block_type_raises():
    with pytest.raises(ValueError, match="unsupported block type"):
        to_append_block_args({"type": "mystery"})
