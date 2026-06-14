from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import Platform
from src.pipeline.classification import ClassificationResult
from src.pipeline.classifier import build_user_message, classify, SYSTEM_PROMPT
from src.pipeline.extracted import Extracted, MediaKind


def _platform() -> Platform:
    return Platform(id="instagram", group="Socials", folder_name="Instagram",
                    hosts=["instagram.com"], extractor="ytdlp")


def _extracted(body: str = "Honey-glazed salmon recipe with photos.") -> Extracted:
    return Extracted(
        title="Honey-glazed salmon",
        body_md=body,
        author="@cookingchannel",
        published_at=None,
        media_kind=MediaKind.IMAGE,
        extra={},
    )


def test_system_prompt_contains_required_instructions():
    """The system prompt must explain JSON output, alias_of semantics, and confidence range."""
    assert "JSON" in SYSTEM_PROMPT
    assert "alias_of" in SYSTEM_PROMPT
    assert "confidence" in SYSTEM_PROMPT.lower()


def test_build_user_message_includes_siblings_and_hints():
    msg = build_user_message(
        extracted=_extracted(),
        platform=_platform(),
        sibling_topics=["Recipes", "Workouts"],
        topic_hints=["Recipes", "Workouts", "Travel", "Architecture", "Memes"],
    )
    assert "instagram" in msg.lower()
    assert "Recipes" in msg
    assert "Workouts" in msg
    assert "Honey-glazed salmon" in msg


def test_build_user_message_truncates_long_body():
    long_body = "X" * 30_000
    msg = build_user_message(
        extracted=_extracted(body=long_body),
        platform=_platform(),
        sibling_topics=[],
        topic_hints=[],
    )
    # Body region should be capped well below 30k.
    assert len(msg) < 15_000


def test_build_user_message_includes_video_summary_when_present():
    """Phase 13: when extracted.extra['video_summary'] is set (vision call
    succeeded), the classifier prompt includes that grounded summary as a
    stronger classification signal than raw transcript alone."""
    extracted = Extracted(
        title="Cooking video",
        body_md="(short transcript)",
        author="@chef",
        published_at=None,
        media_kind=MediaKind.VIDEO,
        extra={
            "video_summary": (
                "A chef demonstrates a knife-skills tutorial focused on "
                "julienne cuts of carrots and celery, with on-screen "
                "annotations of grip and angle."
            ),
        },
    )
    msg = build_user_message(
        extracted=extracted,
        platform=_platform(),
        sibling_topics=[],
        topic_hints=[],
    )
    assert "Vision-grounded summary" in msg
    assert "julienne cuts" in msg
    assert "knife-skills tutorial" in msg


def test_build_user_message_omits_video_summary_block_when_absent():
    """Regression guard: extra={} (or no key) → no grounded block in prompt.
    Backward-compatibility with the golden file + non-video extractors."""
    msg = build_user_message(
        extracted=_extracted(),  # extra={} by default
        platform=_platform(),
        sibling_topics=[],
        topic_hints=[],
    )
    assert "Vision-grounded summary" not in msg


def test_user_message_golden(tmp_path: Path):
    """Compare the assembled prompt against a checked-in golden file."""
    golden = (Path(__file__).parent / "fixtures" / "classifier_prompt_golden.txt").read_text(encoding="utf-8")
    msg = build_user_message(
        extracted=Extracted(
            title="Honey-glazed salmon",
            body_md="Recipe with ingredients and steps.",
            author="@cookingchannel",
            published_at=None,
            media_kind=MediaKind.IMAGE,
            extra={},
        ),
        platform=_platform(),
        sibling_topics=["Recipes", "Workouts"],
        topic_hints=["Recipes", "Workouts", "Travel"],
    )
    assert msg == golden


def _fake_parse_response(result: ClassificationResult) -> MagicMock:
    """messages.parse returns an object whose parsed_output is the validated model."""
    fake = MagicMock()
    fake.parsed_output = result
    return fake


@pytest.mark.asyncio
async def test_classify_returns_parsed_output():
    expected = ClassificationResult(
        topic="Recipes",
        confidence=0.92,
        reasoning="Caption lists ingredients; image shows plated dish.",
        alias_of=None,
    )
    with patch("src.pipeline.classifier.anthropic_client") as Client:
        Client.return_value.messages.parse = AsyncMock(
            return_value=_fake_parse_response(expected)
        )
        result = await classify(
            extracted=_extracted(),
            platform=_platform(),
            sibling_topics=["Recipes"],
            topic_hints=["Recipes", "Workouts"],
        )

    assert isinstance(result, ClassificationResult)
    assert result.topic == "Recipes"
    assert result.confidence == 0.92
    assert result.alias_of is None
    # The schema is enforced server-side via structured outputs.
    call = Client.return_value.messages.parse.await_args
    assert call.kwargs["output_format"] is ClassificationResult


@pytest.mark.asyncio
async def test_classify_handles_alias_of_response():
    expected = ClassificationResult(
        topic="Cooking",
        confidence=0.88,
        reasoning="Recipe content; existing 'Recipes' folder fits.",
        alias_of="Recipes",
    )
    with patch("src.pipeline.classifier.anthropic_client") as Client:
        Client.return_value.messages.parse = AsyncMock(
            return_value=_fake_parse_response(expected)
        )
        result = await classify(
            extracted=_extracted(),
            platform=_platform(),
            sibling_topics=["Recipes"],
            topic_hints=["Recipes"],
        )
    assert result.topic == "Cooking"
    assert result.alias_of == "Recipes"


@pytest.mark.asyncio
async def test_classify_low_confidence_with_null_topic():
    expected = ClassificationResult(
        topic=None,
        confidence=0.4,
        reasoning="Content is ambiguous between Recipes and Memes.",
        alias_of=None,
    )
    with patch("src.pipeline.classifier.anthropic_client") as Client:
        Client.return_value.messages.parse = AsyncMock(
            return_value=_fake_parse_response(expected)
        )
        result = await classify(
            extracted=_extracted(),
            platform=_platform(),
            sibling_topics=[],
            topic_hints=[],
        )
    assert result.topic is None
    assert result.confidence == 0.4


@pytest.mark.asyncio
async def test_classify_raises_when_parsed_output_is_none():
    fake = MagicMock()
    fake.parsed_output = None
    with patch("src.pipeline.classifier.anthropic_client") as Client:
        Client.return_value.messages.parse = AsyncMock(return_value=fake)
        with pytest.raises(RuntimeError, match="parsed_output is None"):
            await classify(
                extracted=_extracted(),
                platform=_platform(),
                sibling_topics=[],
                topic_hints=[],
            )


@pytest.mark.asyncio
async def test_classify_uses_system_prompt_caching():
    expected = ClassificationResult(
        topic="Recipes", confidence=0.9, reasoning="x", alias_of=None,
    )
    with patch("src.pipeline.classifier.anthropic_client") as Client:
        instance = Client.return_value
        instance.messages.parse = AsyncMock(return_value=_fake_parse_response(expected))
        await classify(
            extracted=_extracted(),
            platform=_platform(),
            sibling_topics=[],
            topic_hints=[],
        )
    call = instance.messages.parse.await_args
    system = call.kwargs["system"]
    # System is a list of blocks (cache_control attached) per Anthropic SDK shape
    assert isinstance(system, list)
    assert system[0]["type"] == "text"
    assert system[0].get("cache_control") == {"type": "ephemeral"}
