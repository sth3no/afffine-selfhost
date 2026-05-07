from unittest.mock import MagicMock, patch

import pytest

from src.config import Platform
from src.pipeline.extracted import MediaKind
from src.pipeline.extractors.markitdown_ext import extract


def _platform(id_: str = "article") -> Platform:
    return Platform(id=id_, group="Articles", folder_name="Web", hosts=["*"], extractor="markitdown")


@pytest.mark.asyncio
async def test_markitdown_extracts_html_to_markdown():
    fake_result = MagicMock()
    fake_result.text_content = "# Hello\n\nThis is a test article.\n"
    fake_result.title = "Hello"

    with patch("src.pipeline.extractors.markitdown_ext.MarkItDown") as MD:
        instance = MD.return_value
        instance.convert.return_value = fake_result
        e = await extract("https://example.com/article", _platform())

    assert e.title == "Hello"
    assert "Hello" in e.body_md
    assert e.media_kind == MediaKind.TEXT
    assert e.author is None  # Markitdown doesn't expose author for HTML


@pytest.mark.asyncio
async def test_markitdown_truncates_long_body():
    long_body = "X" * 100_000
    fake_result = MagicMock()
    fake_result.text_content = long_body
    fake_result.title = "Long"

    with patch("src.pipeline.extractors.markitdown_ext.MarkItDown") as MD:
        MD.return_value.convert.return_value = fake_result
        e = await extract("https://example.com/long", _platform())

    assert len(e.body_md) <= 50_000 + 80
    assert e.body_md.endswith("[...truncated]")


@pytest.mark.asyncio
async def test_markitdown_propagates_extraction_errors():
    with patch("src.pipeline.extractors.markitdown_ext.MarkItDown") as MD:
        MD.return_value.convert.side_effect = RuntimeError("network error")
        with pytest.raises(RuntimeError, match="network error"):
            await extract("https://broken.example.com", _platform())
