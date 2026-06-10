"""Tests for the LLM template synthesizer (Sonnet meta-prompt)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.pipeline.extracted import Extracted, MediaKind
from src.pipeline.templates import ContentTemplate
from src.pipeline.template_synth import (
    SynthesizedTemplate,
    META_SYSTEM_PROMPT,
    synthesize_template,
)


def _extracted(**overrides) -> Extracted:
    from datetime import datetime, timezone
    base = dict(
        title="How to bake sourdough",
        body_md="Mix flour, water, starter. Bulk ferment 4-6 hours...",
        author="Bakery",
        published_at=None,
        media_kind=MediaKind.VIDEO,
        extra={"description": "Full recipe and ratios in the description."},
    )
    base.update(overrides)
    return Extracted(**base)


def _template(**overrides) -> ContentTemplate:
    from datetime import datetime, timezone
    base = dict(
        id="t_new",
        platform_id="youtube",
        topic="Recipes",
        name="YouTube Recipe v1",
        system_prompt="You are a recipe summarizer.",
        status="auto",
        generator_meta={"biggest_value": "ingredients + steps"},
        created_by="synth",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return ContentTemplate(**base)


def test_meta_prompt_mentions_required_blocks():
    """The meta-prompt teaches the synthesizer what AFFiNE blocks are
    available. Sanity-check it lists the key flavours."""
    assert "mermaid" in META_SYSTEM_PROMPT.lower()
    assert "embed-html" in META_SYSTEM_PROMPT.lower()
    assert "callout" in META_SYSTEM_PROMPT.lower()
    assert "kf:" in META_SYSTEM_PROMPT
    assert "[[Doc Title]]" in META_SYSTEM_PROMPT or "[[" in META_SYSTEM_PROMPT
    assert "lede" in META_SYSTEM_PROMPT.lower()


@pytest.mark.asyncio
async def test_synthesize_template_calls_sonnet_and_inserts_row():
    fake = MagicMock()
    fake.parsed_output = SynthesizedTemplate(
        name="YouTube Recipe v1",
        system_prompt="You are a recipe summarizer. Produce ingredients list and numbered steps.",
        biggest_value="Ingredients + numbered steps.",
        user_intent="Cook it later.",
        best_roi_format="Ingredients list + numbered steps + time estimate.",
        available_blocks_used=["paragraph", "list", "callout"],
    )
    repo = AsyncMock()
    repo.insert_if_absent = AsyncMock(return_value=_template(platform_id="youtube", topic="Recipes"))

    with patch("src.pipeline.template_synth.anthropic_client") as Client, \
         patch("src.pipeline.template_synth.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.vision_model = "claude-sonnet-4-6"
        Client.return_value.messages.parse = AsyncMock(return_value=fake)

        tmpl = await synthesize_template(
            platform_id="youtube",
            topic="Recipes",
            sample_extracted=_extracted(),
            templates_repo=repo,
        )

    assert tmpl is not None
    assert tmpl.platform_id == "youtube"
    assert tmpl.topic == "Recipes"
    # Sonnet model used for synthesis (not Haiku).
    call = Client.return_value.messages.parse.await_args
    assert call.kwargs["model"] == "claude-sonnet-4-6"
    # Repo received the synthesized prompt + generator_meta.
    repo.insert_if_absent.assert_awaited_once()
    kwargs = repo.insert_if_absent.await_args.kwargs
    assert kwargs["platform_id"] == "youtube"
    assert kwargs["topic"] == "Recipes"
    assert kwargs["system_prompt"] == "You are a recipe summarizer. Produce ingredients list and numbered steps."
    assert kwargs["created_by"] == "synth"
    assert kwargs["status"] == "auto"
    assert "biggest_value" in kwargs["generator_meta"]


@pytest.mark.asyncio
async def test_synthesize_template_passes_sample_in_user_message():
    fake = MagicMock()
    fake.parsed_output = SynthesizedTemplate(
        name="X",
        system_prompt="x" * 50,
        biggest_value="x", user_intent="x", best_roi_format="x",
        available_blocks_used=["paragraph"],
    )
    repo = AsyncMock()
    repo.insert_if_absent = AsyncMock(return_value=_template())

    with patch("src.pipeline.template_synth.anthropic_client") as Client, \
         patch("src.pipeline.template_synth.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.vision_model = "claude-sonnet-4-6"
        instance = Client.return_value
        instance.messages.parse = AsyncMock(return_value=fake)

        await synthesize_template(
            platform_id="youtube",
            topic="Recipes",
            sample_extracted=_extracted(title="UNIQUE_SAMPLE_TITLE_TOKEN"),
            templates_repo=repo,
        )

    user_msg = instance.messages.parse.await_args.kwargs["messages"][0]["content"]
    assert "youtube" in user_msg
    assert "Recipes" in user_msg
    assert "UNIQUE_SAMPLE_TITLE_TOKEN" in user_msg


@pytest.mark.asyncio
async def test_synthesize_template_raises_when_parsed_output_is_none():
    fake = MagicMock()
    fake.parsed_output = None
    repo = AsyncMock()
    with patch("src.pipeline.template_synth.anthropic_client") as Client, \
         patch("src.pipeline.template_synth.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.vision_model = "claude-sonnet-4-6"
        Client.return_value.messages.parse = AsyncMock(return_value=fake)
        with pytest.raises(RuntimeError, match="parsed_output is None"):
            await synthesize_template(
                platform_id="youtube", topic="Recipes",
                sample_extracted=_extracted(), templates_repo=repo,
            )
