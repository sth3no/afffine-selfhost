"""Tests for the title + summary generator."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.pipeline.extracted import Extracted, MediaKind
from src.pipeline.summarizer import SummaryResult, SYSTEM_PROMPT, fallback_title, summarize


def _extracted(**overrides) -> Extracted:
    base = dict(
        title="Some Original Title",
        body_md="A short transcript or description.",
        author="@channel",
        published_at=None,
        media_kind=MediaKind.VIDEO,
        extra={},
    )
    base.update(overrides)
    return Extracted(**base)


# ── Pure helpers ────────────────────────────────────────────────────


def test_system_prompt_mentions_required_constraints():
    assert "JSON" in SYSTEM_PROMPT
    assert "title" in SYSTEM_PROMPT.lower()
    assert "summary" in SYSTEM_PROMPT.lower()


def test_fallback_title_prefers_extracted_title():
    e = _extracted(title="My Title", author=None)
    assert fallback_title(e, url=None) == "My Title"


def test_fallback_title_uses_author_when_no_title():
    e = _extracted(title=None, author="Travis Scott", media_kind=MediaKind.AUDIO)
    assert fallback_title(e, url=None) == "Travis Scott — audio"


def test_fallback_title_uses_url_host():
    e = _extracted(title=None, author=None)
    assert fallback_title(e, url="https://www.instagram.com/reel/abc/?x=1") == "Capture from www.instagram.com"


def test_fallback_title_when_nothing_available():
    e = _extracted(title=None, author=None)
    assert fallback_title(e, url=None) == "Untitled capture"


# ── summarize() — Anthropic call ────────────────────────────────────


@pytest.mark.asyncio
async def test_summarize_parses_clean_json_response():
    fake = MagicMock()
    fake.content = [MagicMock(text=json.dumps({
        "title": "Travis Scott — Mavericks reel",
        "summary_md": "Krátký Instagram reel s úryvkem písně Mavericks od Travise Scotta.",
    }))]

    with patch("src.pipeline.summarizer.AsyncAnthropic") as Client, \
         patch("src.pipeline.summarizer.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-haiku-4-5"
        settings_mock.summarizer_max_body_chars = 4000
        Client.return_value.messages.create = AsyncMock(return_value=fake)

        result = await summarize(_extracted())

    assert isinstance(result, SummaryResult)
    assert result.title == "Travis Scott — Mavericks reel"
    assert "Travise Scotta" in result.summary_md


@pytest.mark.asyncio
async def test_summarize_strips_markdown_code_fence():
    """Models occasionally wrap output in ```json ... ``` despite the prompt — we should handle it."""
    fake = MagicMock()
    fake.content = [MagicMock(text='```json\n{"title": "X", "summary_md": "Y"}\n```')]

    with patch("src.pipeline.summarizer.AsyncAnthropic") as Client, \
         patch("src.pipeline.summarizer.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-haiku-4-5"
        settings_mock.summarizer_max_body_chars = 4000
        Client.return_value.messages.create = AsyncMock(return_value=fake)

        result = await summarize(_extracted())

    assert result.title == "X"
    assert result.summary_md == "Y"


@pytest.mark.asyncio
async def test_summarize_uses_prompt_caching():
    fake = MagicMock()
    fake.content = [MagicMock(text=json.dumps({"title": "X", "summary_md": "Y"}))]

    with patch("src.pipeline.summarizer.AsyncAnthropic") as Client, \
         patch("src.pipeline.summarizer.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-haiku-4-5"
        settings_mock.summarizer_max_body_chars = 4000
        instance = Client.return_value
        instance.messages.create = AsyncMock(return_value=fake)
        await summarize(_extracted())

    call = instance.messages.create.await_args
    system = call.kwargs["system"]
    assert isinstance(system, list)
    assert system[0]["type"] == "text"
    assert system[0].get("cache_control") == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_summarize_truncates_long_body():
    """summarizer_max_body_chars caps the user message body excerpt."""
    fake = MagicMock()
    fake.content = [MagicMock(text=json.dumps({"title": "X", "summary_md": "Y"}))]
    long_body = "X" * 100_000

    with patch("src.pipeline.summarizer.AsyncAnthropic") as Client, \
         patch("src.pipeline.summarizer.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-haiku-4-5"
        settings_mock.summarizer_max_body_chars = 4000
        instance = Client.return_value
        instance.messages.create = AsyncMock(return_value=fake)
        await summarize(_extracted(body_md=long_body))

    call = instance.messages.create.await_args
    user_msg = call.kwargs["messages"][0]["content"]
    # User message should reference the configured cap and be much smaller than 100k.
    assert "first 4000 chars" in user_msg
    assert len(user_msg) < 6000
