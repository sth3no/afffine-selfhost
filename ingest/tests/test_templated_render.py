"""Tests for the templated Haiku render call."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.pipeline.extracted import Extracted, MediaKind
from src.pipeline.templates import ContentTemplate
from src.pipeline.templated_render import TemplatedOutput, fallback_title, render


def _extracted(**overrides) -> Extracted:
    base = dict(
        title="Some Title",
        body_md="Body content.",
        author="@channel",
        published_at=None,
        media_kind=MediaKind.VIDEO,
        extra={},
    )
    base.update(overrides)
    return Extracted(**base)


def _template(**overrides) -> ContentTemplate:
    from datetime import datetime, timezone
    base = dict(
        id="t_test",
        platform_id="youtube",
        topic="Tutorials",
        name="YouTube Tutorial v1",
        system_prompt="You are a tutorial summarizer. Produce numbered steps.",
        status="edited",
        generator_meta=None,
        created_by="user",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return ContentTemplate(**base)


# ── Pure helpers ────────────────────────────────────────────────────


def test_fallback_title_prefers_extracted_title():
    e = _extracted(title="My Title", author=None)
    assert fallback_title(e, url=None) == "My Title"


def test_fallback_title_uses_url_host():
    e = _extracted(title=None, author=None)
    assert fallback_title(e, url="https://www.instagram.com/reel/abc/?x=1") == "Capture from www.instagram.com"


# ── render() shape ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_render_returns_templated_output():
    fake = MagicMock()
    fake.parsed_output = TemplatedOutput(
        title="React Hooks Tutorial",
        lede="useEffect runs after every render unless deps are passed.",
        summary_md="- Hooks replace class lifecycle\n- useState manages local state",
        body_md="## Steps\n1. Import useState\n2. Call inside the component",
    )

    with patch("src.pipeline.templated_render.AsyncAnthropic") as Client, \
         patch("src.pipeline.templated_render.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-haiku-4-5"
        settings_mock.summarizer_max_body_chars = 4000
        Client.return_value.messages.parse = AsyncMock(return_value=fake)

        result = await render(template=_template(), extracted=_extracted(), keyframes=[])

    assert isinstance(result, TemplatedOutput)
    assert result.title == "React Hooks Tutorial"
    assert result.lede is not None
    assert "useState" in result.body_md


@pytest.mark.asyncio
async def test_render_uses_template_system_prompt():
    """The template's system_prompt is what gets sent — not a hardcoded one."""
    fake = MagicMock()
    fake.parsed_output = TemplatedOutput(title="X", lede=None, summary_md="- a", body_md="b")

    with patch("src.pipeline.templated_render.AsyncAnthropic") as Client, \
         patch("src.pipeline.templated_render.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-haiku-4-5"
        settings_mock.summarizer_max_body_chars = 4000
        instance = Client.return_value
        instance.messages.parse = AsyncMock(return_value=fake)

        await render(
            template=_template(system_prompt="UNIQUE_MARKER_PROMPT_TEXT"),
            extracted=_extracted(),
            keyframes=[],
        )

    call = instance.messages.parse.await_args
    system = call.kwargs["system"]
    assert system[0]["text"] == "UNIQUE_MARKER_PROMPT_TEXT"
    assert system[0].get("cache_control") == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_render_includes_description_in_user_message():
    """Description from extractor.extra is surfaced — sources/citations signal."""
    fake = MagicMock()
    fake.parsed_output = TemplatedOutput(title="X", lede=None, summary_md="- a", body_md="b")

    with patch("src.pipeline.templated_render.AsyncAnthropic") as Client, \
         patch("src.pipeline.templated_render.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-haiku-4-5"
        settings_mock.summarizer_max_body_chars = 4000
        instance = Client.return_value
        instance.messages.parse = AsyncMock(return_value=fake)

        await render(
            template=_template(),
            extracted=_extracted(extra={
                "description": "Sources: https://example.com/paper.pdf. Chapter 1: 0:00.",
            }),
            keyframes=[],
        )

    user_msg = instance.messages.parse.await_args.kwargs["messages"][0]["content"]
    assert "Source description" in user_msg
    assert "example.com/paper.pdf" in user_msg


@pytest.mark.asyncio
async def test_render_includes_keyframes_in_user_message():
    fake = MagicMock()
    fake.parsed_output = TemplatedOutput(title="X", lede=None, summary_md="- a", body_md="b")
    keyframes = [
        {"timestamp_seconds": 42.3, "caption": "IDE with React code", "blob_source_id": "blob1"},
        {"timestamp_seconds": 154.0, "caption": "Network tab 200 OK", "blob_source_id": "blob2"},
    ]

    with patch("src.pipeline.templated_render.AsyncAnthropic") as Client, \
         patch("src.pipeline.templated_render.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-haiku-4-5"
        settings_mock.summarizer_max_body_chars = 4000
        instance = Client.return_value
        instance.messages.parse = AsyncMock(return_value=fake)

        await render(template=_template(), extracted=_extracted(), keyframes=keyframes)

    user_msg = instance.messages.parse.await_args.kwargs["messages"][0]["content"]
    assert "Available keyframes" in user_msg
    assert "[0]" in user_msg
    assert "IDE with React code" in user_msg
    assert "[1]" in user_msg
    assert "kf:" in user_msg  # syntax hint to template


@pytest.mark.asyncio
async def test_render_includes_video_summary_when_present():
    fake = MagicMock()
    fake.parsed_output = TemplatedOutput(title="X", lede=None, summary_md="- a", body_md="b")

    with patch("src.pipeline.templated_render.AsyncAnthropic") as Client, \
         patch("src.pipeline.templated_render.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-haiku-4-5"
        settings_mock.summarizer_max_body_chars = 4000
        instance = Client.return_value
        instance.messages.parse = AsyncMock(return_value=fake)

        await render(
            template=_template(),
            extracted=_extracted(extra={"video_summary": "Streaming demo content."}),
            keyframes=[],
        )

    user_msg = instance.messages.parse.await_args.kwargs["messages"][0]["content"]
    assert "Vision-grounded summary" in user_msg
    assert "Streaming demo" in user_msg


@pytest.mark.asyncio
async def test_render_truncates_long_body():
    fake = MagicMock()
    fake.parsed_output = TemplatedOutput(title="X", lede=None, summary_md="- a", body_md="b")

    with patch("src.pipeline.templated_render.AsyncAnthropic") as Client, \
         patch("src.pipeline.templated_render.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-haiku-4-5"
        settings_mock.summarizer_max_body_chars = 4000
        instance = Client.return_value
        instance.messages.parse = AsyncMock(return_value=fake)

        await render(
            template=_template(),
            extracted=_extracted(body_md="X" * 100_000),
            keyframes=[],
        )

    user_msg = instance.messages.parse.await_args.kwargs["messages"][0]["content"]
    assert "first 4000 chars" in user_msg
    assert len(user_msg) < 8000


@pytest.mark.asyncio
async def test_render_raises_when_parsed_output_is_none():
    fake = MagicMock()
    fake.parsed_output = None

    with patch("src.pipeline.templated_render.AsyncAnthropic") as Client, \
         patch("src.pipeline.templated_render.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-haiku-4-5"
        settings_mock.summarizer_max_body_chars = 4000
        Client.return_value.messages.parse = AsyncMock(return_value=fake)

        with pytest.raises(RuntimeError, match="parsed_output is None"):
            await render(template=_template(), extracted=_extracted(), keyframes=[])
