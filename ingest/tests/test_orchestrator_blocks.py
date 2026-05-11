"""Pure-unit tests for the orchestrator's URL-embed helper.

`_build_body_blocks` and `_markdown_to_blocks` were removed from the
orchestrator in Phase 14 (Task 7) — they are now handled by the
template-driven render path (`templated_render` + `markdown_render`).

The `url_embed_block` helper remains in the orchestrator (it is called
from `_replace_doc_body_templated`), so its tests live here.
"""

from src.pipeline.orchestrator import url_embed_block


# ── url_embed_block ─────────────────────────────────────────────────


def test_url_embed_youtube_main_host():
    assert url_embed_block("https://www.youtube.com/watch?v=abc") == {
        "type": "embed-youtube",
        "url": "https://www.youtube.com/watch?v=abc",
    }


def test_url_embed_youtube_short_host():
    assert url_embed_block("https://youtu.be/abc")["type"] == "embed-youtube"


def test_url_embed_youtube_mobile_host():
    assert url_embed_block("https://m.youtube.com/watch?v=abc")["type"] == "embed-youtube"


def test_url_embed_github_repo():
    assert url_embed_block("https://github.com/sth3no/afffine-selfhost")["type"] == "embed-github"


def test_url_embed_figma_design():
    assert url_embed_block("https://www.figma.com/file/abc/Design")["type"] == "embed-figma"


def test_url_embed_loom_video():
    assert url_embed_block("https://www.loom.com/share/abc")["type"] == "embed-loom"


def test_url_embed_falls_back_to_bookmark_for_unknown_host():
    """Instagram, TikTok, X, generic blogs etc. all use bookmark — AFFiNE
    fetches og:image/title/description for the card preview."""
    for url in (
        "https://www.instagram.com/reel/abc/",
        "https://www.tiktok.com/@user/video/123",
        "https://x.com/user/status/123",
        "https://example.com/some/article",
    ):
        assert url_embed_block(url) == {"type": "bookmark", "url": url}, url
