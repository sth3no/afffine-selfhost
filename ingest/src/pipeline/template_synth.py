"""Template synthesizer — Sonnet 4.6 meta-prompt designs a template
for a (platform, topic) pair we haven't seen before.

Called from the orchestrator when `TemplatesRepository.resolve()` returns
None at every fallback level. Saves with `insert_if_absent` so concurrent
synthesis races resolve to a single winner.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator

from src.config import settings
from src.llm_clients import anthropic_client
from src.llm_usage import record_anthropic_usage
from src.pipeline.extracted import Extracted
from src.pipeline.templates import ContentTemplate, TemplatesRepository

log = logging.getLogger(__name__)


class SynthesizedTemplate(BaseModel):
    """Sonnet's output: a name + the system prompt to use for this scope,
    plus reflective fields stored on `generator_meta` for audit."""

    name: str = Field(description="Human-readable label, 2-6 words.")
    system_prompt: str = Field(
        description="The complete system prompt that Haiku will receive for every "
                    "future capture matching this (platform, topic) scope. Self-contained."
    )
    biggest_value: str = Field(description="What's the biggest value in this kind of content?")
    user_intent: str = Field(description="What does the user actually want when they save one?")
    best_roi_format: str = Field(description="Best ROI format — what should body_md look like?")
    available_blocks_used: list[str] = Field(
        description="Which AFFiNE block flavours the generated prompt actively instructs."
    )

    @field_validator("system_prompt")
    @classmethod
    def _prompt_must_be_substantive(cls, v: str) -> str:
        if len(v) < 50:
            raise ValueError(
                f"synthesized system_prompt is too short ({len(v)} chars); "
                "expected at least 50 chars of instruction."
            )
        return v


META_SYSTEM_PROMPT = """You are designing a content template for a personal
knowledge-base ingestion pipeline. Each captured URL of a given
(platform, topic) kind will be summarized into an AFFiNE document. Your
job: design the system prompt that will run for every future capture
matching this scope.

You will be given:
- The platform (e.g., youtube, instagram, arxiv)
- The topic (e.g., Tutorials, Recipes, Documentary)
- One sample capture's extracted content (title, author, description,
  transcript/body, vision summary if present, keyframes available)

Ask yourself, in this order:
1. What is the biggest value in this kind of content for the user?
2. What does the user actually want when they save one of these — what
   are they going to look at again in 6 months?
3. What's the best ROI in text form — what should `body_md` look like
   to maximize signal per scroll?
4. Which of the available AFFiNE block flavours best express that?

Available block flavours the generated prompt can request (via markdown):
- Headings h1-h6: `# heading`, `## heading`
- Paragraphs: plain text
- Bulleted/numbered/todo lists: `- item`, `1. item`, `[ ] item`
- Code blocks with language: fenced ```python (or any language)``` blocks
- Mermaid diagrams: fenced ```mermaid``` blocks containing flowchart / sequence /
  gantt / mindmap syntax (rendered as a diagram by the AFFiNE renderer)
- Embedded HTML "frames" (SVG charts, styled cards): fenced ```embed-html```
  blocks containing inline HTML/SVG (rendered as a styled card)
- Image refs to available keyframes: `![caption](kf:<index>)`
- Cross-doc references: `[[Doc Title]]` (resolves to embed-linked-doc)
- Callouts (highlighted blocks): `> [!callout] text`
- URL embeds: paste `[](url)`; renderer picks youtube/github/figma/loom
  or falls back to bookmark
- Dividers: `---`

Rules the generated prompt MUST always include:
- Title rule: 1-10 words, English default, Czech/Slovak preserved.
- Lede rule: if source title is a question/mystery/clickbait, populate
  `lede` with one direct answering sentence; else null.
- Summary rule: 3-6 bullets, one short line each, no intro/outro.
- Description rule: mine `extracted.extra.description` for sources,
  citations, related links, chapter markers. Surface them in `body_md`
  (typically `## Sources` section). Strip sponsor/social noise.
- Body rule: tailored to this content type (your design).
- Language rule: English by default; Czech/Slovak preserved if source is.

Return JSON matching the SynthesizedTemplate schema. The `system_prompt`
you generate will be sent to Haiku 4.5 — make it self-contained.
"""


def _build_user_message(
    *, platform_id: str, topic: str, sample: Extracted
) -> str:
    body_excerpt = sample.body_md[:4000]
    description = (sample.extra or {}).get("description")
    video_summary = (sample.extra or {}).get("video_summary")
    parts: list[str] = [
        f"Platform: {platform_id}",
        f"Topic: {topic}",
        "",
        "Sample capture:",
        f"- Title: {sample.title or '(none)'}",
        f"- Author: {sample.author or '(unknown)'}",
        f"- Media kind: {sample.media_kind.value}",
        "",
    ]
    if description:
        parts += ["Description from publisher:", str(description), ""]
    if video_summary:
        parts += ["Vision-grounded summary:", str(video_summary), ""]
    parts += ["Body excerpt:", body_excerpt]
    return "\n".join(parts)


async def synthesize_template(
    *,
    platform_id: str,
    topic: str,
    sample_extracted: Extracted,
    templates_repo: TemplatesRepository,
) -> ContentTemplate:
    """Run the Sonnet meta-prompt, persist with insert_if_absent, return the row.

    Concurrent calls for the same (platform_id, topic) resolve to a single
    winner via the partial UNIQUE index on `content_templates`.
    """
    client = anthropic_client()
    user_msg = _build_user_message(
        platform_id=platform_id, topic=topic, sample=sample_extracted
    )

    response = await client.messages.parse(
        model=settings.vision_model,  # Sonnet 4.6 — runs once per scope
        max_tokens=4096,
        system=[{
            "type": "text",
            "text": META_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_msg}],
        output_format=SynthesizedTemplate,
    )
    record_anthropic_usage(response, kind="template_synth", model=settings.vision_model)

    if response.parsed_output is None:
        raise RuntimeError(
            "template_synth: parsed_output is None — schema-enforced parse failed"
        )

    synth = response.parsed_output
    generator_meta = {
        "biggest_value": synth.biggest_value,
        "user_intent": synth.user_intent,
        "best_roi_format": synth.best_roi_format,
        "available_blocks_used": synth.available_blocks_used,
        "synthesizer_model": settings.vision_model,
        "synthesized_at": datetime.now(timezone.utc).isoformat(),
    }

    return await templates_repo.insert_if_absent(
        platform_id=platform_id,
        topic=topic,
        name=synth.name,
        system_prompt=synth.system_prompt,
        status="auto",
        created_by="synth",
        generator_meta=generator_meta,
    )
