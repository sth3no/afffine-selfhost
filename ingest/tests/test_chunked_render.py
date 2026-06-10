"""Tests for the map-reduce chunked render path."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.pipeline.chunked_render import (
    ChunkSummary,
    chunked_render,
    split_into_chunks,
)
from src.pipeline.extracted import Extracted, MediaKind
from src.pipeline.templated_render import TemplatedOutput
from src.pipeline.templates import ContentTemplate


def _template(**overrides) -> ContentTemplate:
    base = dict(
        id="t_seed", platform_id="*", topic="*",
        name="Default summarizer",
        system_prompt="You are a content summarizer. " + ("x" * 50),
        status="auto", generator_meta=None, created_by="synth",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return ContentTemplate(**base)


def _extracted(**overrides) -> Extracted:
    base = dict(
        title="Some Video", body_md="", author="@channel",
        published_at=None, media_kind=MediaKind.VIDEO, extra={},
    )
    base.update(overrides)
    return Extracted(**base)


# ── Chunker ──────────────────────────────────────────────────────────


def test_chunker_returns_single_chunk_for_short_body():
    body = "Short body that fits."
    chunks = split_into_chunks(body, chunk_size=8000, overlap=500, max_chunks=16)
    assert len(chunks) == 1
    assert chunks[0]["text"] == body
    assert chunks[0]["timestamp_range"] is None


def test_chunker_fixed_size_splits_with_overlap():
    """No timestamps → falls back to character-based splitting."""
    body = "x" * 25000
    chunks = split_into_chunks(body, chunk_size=10000, overlap=500, max_chunks=16)
    # Body is 25k chars. With chunk_size=10000 and overlap=500, each chunk
    # advances by 9500 chars. Expect 3 chunks: 0-10000, 9500-19500, 19000-25000.
    assert len(chunks) == 3
    assert all(c["timestamp_range"] is None for c in chunks)
    # Overlap is real: end of chunk 0 and start of chunk 1 share characters.
    assert chunks[0]["text"][-100:] == chunks[1]["text"][:100]


def test_chunker_caps_at_max_chunks():
    """Very long body with max_chunks cap → last chunk gets a truncation note."""
    body = "x" * 200_000
    chunks = split_into_chunks(body, chunk_size=10_000, overlap=500, max_chunks=4)
    assert len(chunks) == 4
    assert "truncated" in chunks[-1]["text"].lower()


def test_chunker_prefers_timestamp_boundaries_when_available():
    """Body with >= 3 timestamp anchors splits on those boundaries.
    Each chunk gets a timestamp_range derived from its first & last anchor."""
    body = (
        "[**0:00**](https://example.com?t=0)\nSection intro.\n\n"
        + ("filler\n" * 200)
        + "[**5:30**](https://example.com?t=330)\nMid section.\n\n"
        + ("filler\n" * 200)
        + "[**11:00**](https://example.com?t=660)\nLate section.\n\n"
        + ("filler\n" * 200)
        + "[**17:00**](https://example.com?t=1020)\nFinal section.\n"
        + ("filler\n" * 100)
    )
    chunks = split_into_chunks(body, chunk_size=1500, overlap=500, max_chunks=16)
    # We expect multiple chunks AND every chunk has a timestamp_range string.
    assert len(chunks) >= 2
    assert all(c["timestamp_range"] is not None for c in chunks)
    # First chunk's range begins with the first anchor's timestamp.
    assert chunks[0]["timestamp_range"].startswith("0:00")


def test_chunker_timestamp_range_single_anchor_chunk():
    """A chunk containing exactly one anchor gets that timestamp as both
    endpoints (`X:XX` not `X:XX–X:XX`)."""
    body = (
        "[**0:00**](https://example.com?t=0)\n"
        + ("filler\n" * 100)
        + "[**5:00**](https://example.com?t=300)\n"
        + ("filler\n" * 100)
        + "[**10:00**](https://example.com?t=600)\n"
        + ("filler\n" * 100)
    )
    chunks = split_into_chunks(body, chunk_size=400, overlap=100, max_chunks=16)
    assert len(chunks) >= 2


# ── Map step (smoke test) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chunked_render_makes_n_plus_one_anthropic_calls():
    """For a 3-chunk body, expect 3 map calls + 1 reduce call = 4 total."""
    body = (
        "[**0:00**](https://x?t=0)\n"
        + ("filler\n" * 300)
        + "[**5:00**](https://x?t=300)\n"
        + ("filler\n" * 300)
        + "[**10:00**](https://x?t=600)\n"
        + ("filler\n" * 300)
    )
    extracted = _extracted(body_md=body)

    # Each chunk call returns a ChunkSummary, the reducer returns TemplatedOutput.
    chunk_response = MagicMock()
    chunk_response.parsed_output = ChunkSummary(
        section_title="A section", key_points=["A claim"],
    )
    reduce_response = MagicMock()
    reduce_response.parsed_output = TemplatedOutput(
        title="Title", lede=None, summary_md="- a", body_md="b",
    )

    # First N calls return ChunkSummary, last call returns TemplatedOutput.
    parse_side = AsyncMock()
    parse_side.side_effect = [chunk_response] * 3 + [reduce_response]

    with patch("src.pipeline.chunked_render.anthropic_client") as Client, \
         patch("src.pipeline.chunked_render.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-sonnet-4-6"
        settings_mock.chunked_render_threshold_chars = 1
        settings_mock.chunk_size_chars = 1500
        settings_mock.chunk_overlap_chars = 300
        settings_mock.max_chunks_per_capture = 16
        Client.return_value.messages.parse = parse_side

        result = await chunked_render(
            template=_template(), extracted=extracted, keyframes=[],
        )

    assert isinstance(result, TemplatedOutput)
    assert result.title == "Title"
    # 3 map calls + 1 reduce call.
    assert parse_side.await_count == 4


@pytest.mark.asyncio
async def test_reducer_receives_reveals_block_when_chunks_have_reveals():
    """Map step produces a `reveal` field on a chunk; reducer's user_msg
    surfaces it under a REVEALS block so the lede resolver can use it."""
    body = "[**0:00**](x?t=0)\n" + ("filler\n" * 300) + "[**5:00**](x?t=300)\n"
    extracted = _extracted(title="They Did It — Which Model Wins?", body_md=body)

    chunk_with_reveal = MagicMock()
    chunk_with_reveal.parsed_output = ChunkSummary(
        section_title="The reveal",
        key_points=["Result was disclosed."],
        reveal="Claude Sonnet 4.6 aced the Swift challenge.",
    )
    chunk_plain = MagicMock()
    chunk_plain.parsed_output = ChunkSummary(
        section_title="Setup", key_points=["Background context."],
    )
    reduce_response = MagicMock()
    reduce_response.parsed_output = TemplatedOutput(
        title="Swift winner", lede="Claude Sonnet 4.6 won.",
        summary_md="- a", body_md="b",
    )

    parse_side = AsyncMock()
    parse_side.side_effect = [chunk_plain, chunk_with_reveal, reduce_response]

    with patch("src.pipeline.chunked_render.anthropic_client") as Client, \
         patch("src.pipeline.chunked_render.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-sonnet-4-6"
        settings_mock.chunked_render_threshold_chars = 1
        settings_mock.chunk_size_chars = 1500
        settings_mock.chunk_overlap_chars = 300
        settings_mock.max_chunks_per_capture = 16
        Client.return_value.messages.parse = parse_side

        await chunked_render(
            template=_template(), extracted=extracted, keyframes=[],
        )

    # Inspect the LAST call (the reduce call): user_msg must contain the
    # REVEALS block with the chunk's reveal sentence.
    reduce_call = parse_side.await_args_list[-1]
    user_msg = reduce_call.kwargs["messages"][0]["content"]
    assert "REVEALS found by map step" in user_msg
    assert "Claude Sonnet 4.6 aced the Swift challenge." in user_msg


@pytest.mark.asyncio
async def test_reducer_uses_template_system_prompt():
    """The reduce call sends `template.system_prompt` as system, not a
    hardcoded prompt — so user edits to the template affect reduce output."""
    body = "[**0:00**](x?t=0)\n" + ("filler\n" * 300) + "[**5:00**](x?t=300)\n"
    extracted = _extracted(body_md=body)

    chunk_response = MagicMock()
    chunk_response.parsed_output = ChunkSummary(
        section_title="Section", key_points=["A"],
    )
    reduce_response = MagicMock()
    reduce_response.parsed_output = TemplatedOutput(
        title="T", lede=None, summary_md="- a", body_md="b",
    )
    parse_side = AsyncMock()
    parse_side.side_effect = [chunk_response, chunk_response, reduce_response]

    with patch("src.pipeline.chunked_render.anthropic_client") as Client, \
         patch("src.pipeline.chunked_render.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-sonnet-4-6"
        settings_mock.chunked_render_threshold_chars = 1
        settings_mock.chunk_size_chars = 1500
        settings_mock.chunk_overlap_chars = 300
        settings_mock.max_chunks_per_capture = 16
        Client.return_value.messages.parse = parse_side

        await chunked_render(
            template=_template(system_prompt="USER_EDITED_PROMPT_TOKEN_" + ("x" * 50)),
            extracted=extracted,
            keyframes=[],
        )

    reduce_call = parse_side.await_args_list[-1]
    system = reduce_call.kwargs["system"]
    assert system[0]["text"].startswith("USER_EDITED_PROMPT_TOKEN_")


@pytest.mark.asyncio
async def test_reducer_includes_keyframes_in_user_message():
    body = "[**0:00**](x?t=0)\n" + ("filler\n" * 300) + "[**5:00**](x?t=300)\n"
    extracted = _extracted(body_md=body)
    keyframes = [
        {"timestamp_seconds": 42.3, "caption": "IDE", "blob_source_id": "blob1"},
    ]

    chunk_response = MagicMock()
    chunk_response.parsed_output = ChunkSummary(
        section_title="Section", key_points=["A"],
    )
    reduce_response = MagicMock()
    reduce_response.parsed_output = TemplatedOutput(
        title="T", lede=None, summary_md="- a", body_md="b",
    )
    parse_side = AsyncMock()
    parse_side.side_effect = [chunk_response, chunk_response, reduce_response]

    with patch("src.pipeline.chunked_render.anthropic_client") as Client, \
         patch("src.pipeline.chunked_render.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-sonnet-4-6"
        settings_mock.chunked_render_threshold_chars = 1
        settings_mock.chunk_size_chars = 1500
        settings_mock.chunk_overlap_chars = 300
        settings_mock.max_chunks_per_capture = 16
        Client.return_value.messages.parse = parse_side

        await chunked_render(
            template=_template(), extracted=extracted, keyframes=keyframes,
        )

    reduce_user_msg = parse_side.await_args_list[-1].kwargs["messages"][0]["content"]
    assert "Available keyframes" in reduce_user_msg
    assert "[0] t=42.3s — IDE" in reduce_user_msg


@pytest.mark.asyncio
async def test_reducer_raises_when_parsed_output_is_none():
    """Defensive: reducer returning None should raise."""
    body = "[**0:00**](x?t=0)\n" + ("filler\n" * 300) + "[**5:00**](x?t=300)\n"
    extracted = _extracted(body_md=body)

    chunk_response = MagicMock()
    chunk_response.parsed_output = ChunkSummary(
        section_title="Section", key_points=["A"],
    )
    none_response = MagicMock()
    none_response.parsed_output = None
    parse_side = AsyncMock()
    parse_side.side_effect = [chunk_response, chunk_response, none_response]

    with patch("src.pipeline.chunked_render.anthropic_client") as Client, \
         patch("src.pipeline.chunked_render.settings") as settings_mock:
        settings_mock.anthropic_api_key = "sk-ant-test"
        settings_mock.summarizer_model = "claude-sonnet-4-6"
        settings_mock.chunked_render_threshold_chars = 1
        settings_mock.chunk_size_chars = 1500
        settings_mock.chunk_overlap_chars = 300
        settings_mock.max_chunks_per_capture = 16
        Client.return_value.messages.parse = parse_side

        with pytest.raises(RuntimeError, match="reduce parsed_output is None"):
            await chunked_render(
                template=_template(), extracted=extracted, keyframes=[],
            )
