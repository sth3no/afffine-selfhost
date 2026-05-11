"""Templated render call — Haiku 4.5 with a per-template system prompt.

Replaces the fixed `summarizer.py`. The template comes from the
`content_templates` table (or LLM synthesis); its `system_prompt` is
sent verbatim with `cache_control: ephemeral` so the prefix cache hits
across consecutive captures of the same kind.

Returns a strict `TemplatedOutput { title, lede, summary_md, body_md }`
via `messages.parse(output_format=TemplatedOutput)`.
"""

from __future__ import annotations

import logging
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field

from src.config import settings
from src.pipeline.extracted import Extracted
from src.pipeline.templates import ContentTemplate

log = logging.getLogger(__name__)


class TemplatedOutput(BaseModel):
    """Strict shape Claude must return — enforced by structured-outputs."""

    title: str = Field(
        description=(
            "Short descriptive title (1-10 words). No URL, no brackets. "
            "Default language ENGLISH; if the source content is Czech or "
            "Slovak, keep the title in that language."
        ),
    )
    lede: str | None = Field(
        default=None,
        description=(
            "ONE sentence that directly answers a clickbait/teaser title "
            "(who/what/why). Populate when the source title is a question, "
            "mystery, exaggeration, or clickbait. Otherwise leave null."
        ),
    )
    summary_md: str = Field(
        description=(
            "Markdown bulleted list (3-6 items) of the most exciting, "
            "surprising, or actionable things. Each bullet on its own line, "
            "starts with '- '. NO intro/outro prose."
        ),
    )
    body_md: str = Field(
        description=(
            "Template-specific structured markdown body. Headings, lists, "
            "code blocks, mermaid, embed-html, kf:<n> image refs, "
            "[[Doc Title]] cross-refs all allowed. Rendered downstream."
        ),
    )


def _build_user_message(extracted: Extracted, keyframes: list[dict[str, Any]]) -> str:
    body_excerpt = extracted.body_md[: settings.summarizer_max_body_chars]
    description = (extracted.extra or {}).get("description")
    video_summary = (extracted.extra or {}).get("video_summary")
    published = getattr(extracted, "published_at", None)

    parts: list[str] = [
        "Captured content:",
        f"- Original title: {extracted.title or '(none)'}",
        f"- Author/channel: {extracted.author or '(unknown)'}",
        f"- Media kind: {extracted.media_kind.value}",
        f"- Published: {published or '(unknown)'}",
        "",
    ]

    if description:
        parts.append(
            "Source description (from publisher — may contain sources, "
            "chapter markers, sponsor links, related content; extract valuable "
            "references, strip noise):"
        )
        parts.append(str(description))
        parts.append("")

    if video_summary:
        parts.append("Vision-grounded summary (transcript + keyframes):")
        parts.append(str(video_summary))
        parts.append("")

    if keyframes:
        parts.append(
            "Available keyframes (reference by index, e.g. ![caption](kf:0)):"
        )
        for i, kf in enumerate(keyframes):
            ts = kf.get("timestamp_seconds", 0.0)
            caption = (kf.get("caption") or "").strip()
            parts.append(f"  [{i}] t={ts:.1f}s — {caption}")
        parts.append("")

    parts.append(
        f"Body excerpt (truncated to first {settings.summarizer_max_body_chars} chars):"
    )
    parts.append("")
    parts.append(body_excerpt)
    return "\n".join(parts)


async def render(
    *,
    template: ContentTemplate,
    extracted: Extracted,
    keyframes: list[dict[str, Any]],
) -> TemplatedOutput:
    """Single Haiku call → TemplatedOutput."""
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    user_msg = _build_user_message(extracted, keyframes)

    response = await client.messages.parse(
        model=settings.summarizer_model,
        max_tokens=2048,
        system=[
            {
                "type": "text",
                "text": template.system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_msg}],
        output_format=TemplatedOutput,
    )

    if response.parsed_output is None:
        raise RuntimeError(
            "templated_render: parsed_output is None — schema-enforced parse "
            "failed; check summarizer_model supports structured outputs"
        )
    return response.parsed_output


def fallback_title(extracted: Extracted, *, url: str | None) -> str:
    """Deterministic title used when no API key is present or the LLM call
    fails to parse. Pure function — no I/O, no LLM."""
    if extracted.title:
        return extracted.title.strip()
    if extracted.author:
        return f"{extracted.author} — {extracted.media_kind.value}"
    if url:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or url
        return f"Capture from {host}"
    return "Untitled capture"
