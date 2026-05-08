"""Anthropic-based title + summary generator.

Runs after extraction, before classification. Generates:
  - title: a short, descriptive title (no URL, no brackets)
  - summary_md: a 2-3 sentence summary of the content

Uses Claude Haiku 4.5 by default (matching the existing classifier choice
for cost-conscious per-capture LLM calls). Override via SUMMARIZER_MODEL
to switch to Opus 4.7 / Sonnet 4.6 if quality matters more than cost.

Schema is enforced via `messages.parse(output_format=SummaryResult)` —
Anthropic's structured-outputs feature guarantees the response matches
the Pydantic model, so the model can't silently drop a field. Without
this, Haiku occasionally returned just `{"title": "..."}` and the
Pydantic validation tripped on the missing `summary_md`.

The system prompt is marked cache_control: ephemeral so successive calls
within the 5-minute window reuse the prefix.
"""

from __future__ import annotations

import logging

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field

from src.config import settings
from src.pipeline.extracted import Extracted

log = logging.getLogger(__name__)


class SummaryResult(BaseModel):
    """Strict shape Claude must return — enforced by structured-outputs."""

    title: str = Field(
        description=(
            "Short descriptive title (1-10 words). No URL, no brackets. "
            "Captures the GIST of the source (artist + song, recipe name, "
            "talk topic). Match content language: if content is Czech, "
            "title is Czech."
        ),
    )
    summary_md: str = Field(
        description=(
            "2-3 sentence summary in plain markdown. Match content language. "
            "Focus on WHAT the content is about and WHY it matters, not on "
            "metadata fields like duration or author."
        ),
    )


SYSTEM_PROMPT = """You are a content summarizer for a personal knowledge base.
For each captured social-media or web post, generate a concise descriptive
title and a brief content summary.

Title rules:
- 1-10 words, no URL, no enclosing brackets/quotes
- Capture the GIST of the source (e.g. "Travis Scott — Mavericks reel",
  "Italian carbonara recipe", "GPT-4 jailbreak demo")
- Match content language. If the transcript is Czech, the title is Czech.
- Title Case for English; sentence case for Czech.

Summary rules:
- 2-3 sentences, plain markdown (no headings)
- Match content language
- Describe WHAT the content is and WHY it matters, not duration/author
- If transcript is profane/explicit, summarize content neutrally without
  reproducing slurs

Return STRICT JSON only — no prose, no markdown code fences.
"""


async def summarize(extracted: Extracted) -> SummaryResult:
    """Single Anthropic call → SummaryResult.

    Uses `messages.parse(output_format=SummaryResult)` — Anthropic's
    structured-outputs feature enforces the schema server-side, so
    `parsed_output` is guaranteed to be a fully-populated SummaryResult.
    """
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    body_excerpt = (extracted.body_md or "")[: settings.summarizer_max_body_chars]
    user_msg = (
        f"Captured content metadata:\n"
        f"- Original title: {extracted.title or '(none)'}\n"
        f"- Author/channel: {extracted.author or '(unknown)'}\n"
        f"- Media kind: {extracted.media_kind.value}\n"
        f"\n"
        f"Body excerpt (truncated to first {settings.summarizer_max_body_chars} chars):\n"
        f"\n"
        f"{body_excerpt}\n"
    )

    response = await client.messages.parse(
        model=settings.summarizer_model,
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_msg}],
        output_format=SummaryResult,
    )

    if response.parsed_output is None:
        raise RuntimeError(
            "summarizer: parsed_output is None — schema-enforced parse failed; "
            "check classifier_model supports structured outputs",
        )
    return response.parsed_output


def fallback_title(extracted: Extracted, *, url: str | None) -> str:
    """When summarization is unavailable (no API key, parse fail), derive a
    reasonable title without a remote call.

    Priority:
      1. Original extracted.title (yt-dlp metadata)
      2. extracted.author + media kind
      3. URL host (last resort, still better than the raw URL)
    """
    if extracted.title:
        return extracted.title.strip()
    if extracted.author:
        return f"{extracted.author} — {extracted.media_kind.value}"
    if url:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or url
        return f"Capture from {host}"
    return "Untitled capture"
