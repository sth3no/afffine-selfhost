"""Tests for the title + summary generator."""

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


# ── summarize() — Anthropic structured-output parse ─────────────────


@pytest.mark.asyncio
async def test_summarize_returns_parsed_output():
    """messages.parse() with output_format guarantees a typed parsed_output."""
    fake = MagicMock()
    fake.parsed_output = SummaryResult(
        title="Travis Scott — Mavericks reel",
        summary_md="Krátký Instagram reel s úryvkem písně Mavericks od Travise Scotta.",
    )

    with patch("src.pipeline.summarizer.AsyncAnthropic") as Client, \
         patch("src.pipeline.summarizer.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-haiku-4-5"
        settings_mock.summarizer_max_body_chars = 4000
        Client.return_value.messages.parse = AsyncMock(return_value=fake)

        result = await summarize(_extracted())

    assert isinstance(result, SummaryResult)
    assert result.title == "Travis Scott — Mavericks reel"
    assert "Travise Scotta" in result.summary_md


@pytest.mark.asyncio
async def test_summarize_passes_output_format_pydantic_class():
    """The Pydantic class is forwarded as output_format so Anthropic enforces
    the schema server-side. This is what prevents the 'missing summary_md'
    bug we saw on production."""
    fake = MagicMock()
    fake.parsed_output = SummaryResult(title="X", summary_md="Y")

    with patch("src.pipeline.summarizer.AsyncAnthropic") as Client, \
         patch("src.pipeline.summarizer.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-haiku-4-5"
        settings_mock.summarizer_max_body_chars = 4000
        instance = Client.return_value
        instance.messages.parse = AsyncMock(return_value=fake)
        await summarize(_extracted())

    call = instance.messages.parse.await_args
    assert call.kwargs["output_format"] is SummaryResult


@pytest.mark.asyncio
async def test_summarize_uses_prompt_caching():
    fake = MagicMock()
    fake.parsed_output = SummaryResult(title="X", summary_md="Y")

    with patch("src.pipeline.summarizer.AsyncAnthropic") as Client, \
         patch("src.pipeline.summarizer.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-haiku-4-5"
        settings_mock.summarizer_max_body_chars = 4000
        instance = Client.return_value
        instance.messages.parse = AsyncMock(return_value=fake)
        await summarize(_extracted())

    call = instance.messages.parse.await_args
    system = call.kwargs["system"]
    assert isinstance(system, list)
    assert system[0]["type"] == "text"
    assert system[0].get("cache_control") == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_summarize_truncates_long_body():
    """summarizer_max_body_chars caps the user message body excerpt."""
    fake = MagicMock()
    fake.parsed_output = SummaryResult(title="X", summary_md="Y")
    long_body = "X" * 100_000

    with patch("src.pipeline.summarizer.AsyncAnthropic") as Client, \
         patch("src.pipeline.summarizer.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-haiku-4-5"
        settings_mock.summarizer_max_body_chars = 4000
        instance = Client.return_value
        instance.messages.parse = AsyncMock(return_value=fake)
        await summarize(_extracted(body_md=long_body))

    call = instance.messages.parse.await_args
    user_msg = call.kwargs["messages"][0]["content"]
    # User message should reference the configured cap and be much smaller than 100k.
    assert "first 4000 chars" in user_msg
    assert len(user_msg) < 6000


@pytest.mark.asyncio
async def test_summarize_raises_when_parsed_output_is_none():
    """parsed_output=None means the schema-enforced parse failed (e.g. model
    doesn't support structured outputs). Surface a clear error rather than
    crashing later on attribute access."""
    fake = MagicMock()
    fake.parsed_output = None

    with patch("src.pipeline.summarizer.AsyncAnthropic") as Client, \
         patch("src.pipeline.summarizer.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-haiku-4-5"
        settings_mock.summarizer_max_body_chars = 4000
        Client.return_value.messages.parse = AsyncMock(return_value=fake)

        with pytest.raises(RuntimeError, match="parsed_output is None"):
            await summarize(_extracted())
