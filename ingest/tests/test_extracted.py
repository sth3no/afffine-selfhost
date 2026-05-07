from datetime import datetime, timezone

from src.pipeline.extracted import Extracted, MediaKind, truncate_body


def test_extracted_has_required_fields():
    e = Extracted(
        title="Hello",
        body_md="# body",
        author="author",
        published_at=datetime(2026, 5, 7, tzinfo=timezone.utc),
        media_kind=MediaKind.TEXT,
        extra={"channel": "@x"},
    )
    assert e.title == "Hello"
    assert e.body_md == "# body"
    assert e.author == "author"
    assert e.media_kind == MediaKind.TEXT
    assert e.extra == {"channel": "@x"}


def test_extracted_optional_fields_default_to_none():
    e = Extracted(title=None, body_md="body", author=None, published_at=None, media_kind=MediaKind.VIDEO, extra={})
    assert e.title is None
    assert e.author is None
    assert e.published_at is None


def test_media_kind_values():
    assert MediaKind.TEXT.value == "text"
    assert MediaKind.VIDEO.value == "video"
    assert MediaKind.AUDIO.value == "audio"
    assert MediaKind.IMAGE.value == "image"
    assert MediaKind.MIXED.value == "mixed"


def test_truncate_body_under_limit_passes_through():
    assert truncate_body("hello", limit=100) == "hello"


def test_truncate_body_over_limit_appends_marker():
    body = "x" * 200
    out = truncate_body(body, limit=50)
    assert len(out) <= 50 + 80  # marker is short
    assert out.endswith("[...truncated]")


def test_truncate_body_at_exact_limit_no_marker():
    body = "x" * 50
    assert truncate_body(body, limit=50) == body
