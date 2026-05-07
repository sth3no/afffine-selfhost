"""Anthropic Haiku 4.5 classifier with prompt caching.

Single call per Extracted record. The system prompt explains the JSON
contract, the alias_of mechanism, and the confidence floor; it's marked
with cache_control: ephemeral so subsequent calls reuse the prefix.

The user message is fresh per call: platform, existing siblings, topic
hints, and the captured content excerpt.
"""

from __future__ import annotations

import json
from typing import Any

from anthropic import AsyncAnthropic

from src.config import Platform, settings
from src.pipeline.classification import ClassificationResult
from src.pipeline.extracted import Extracted


SYSTEM_PROMPT = """You are a content classifier for a personal knowledge base.
Your job is to assign a topic folder to a captured social-media or web post.

You will be given:
- The platform (e.g., Instagram, YouTube, X, arXiv)
- The list of existing topic folders under that platform
- A list of suggested topics for that platform (a hint, not a constraint)
- The captured content (title, author, media kind, body excerpt)

Output strict JSON with exactly these keys:
{
  "topic": string | null,        // The chosen topic name. null if confidence < 0.6.
  "confidence": number,          // 0.0 to 1.0.
  "reasoning": string,           // 1-2 sentences. What in the content drove the choice.
  "alias_of": string | null      // If your proposed topic is a duplicate of an existing
                                  // sibling (e.g., you propose "Cooking" and "Recipes"
                                  // already exists), set alias_of to the existing name.
}

Guidelines:
- PREFER reusing an existing sibling topic when content fits.
- Only propose a NEW topic when content is clearly distinct from all siblings.
- If the proposed new topic is semantically similar to an existing sibling
  (e.g., Cooking vs Recipes, Workouts vs Fitness), set alias_of to the
  existing name and the system will collapse them.
- Keep topic names short (1-2 words), Title Case.
- Confidence below 0.6 means topic should be null and the doc stays
  unfiled at the platform root for later review.

Return ONLY the JSON object. No prose, no markdown fences.
"""


def build_user_message(
    *,
    extracted: Extracted,
    platform: Platform,
    sibling_topics: list[str],
    topic_hints: list[str],
) -> str:
    siblings_block = (
        "\n".join(f"- {s}" for s in sibling_topics)
        if sibling_topics
        else "(none — this would be the first topic folder under this platform)"
    )
    hints_block = ", ".join(topic_hints) if topic_hints else "(no hints configured)"
    body_excerpt = (extracted.body_md or "")[:8000]

    return (
        f"Platform: {platform.id} ({platform.group}/{platform.folder_name})\n"
        f"\n"
        f"Existing topic folders under Sources/{platform.group}/{platform.folder_name}/:\n"
        f"{siblings_block}\n"
        f"\n"
        f"Suggested topics for this platform (you may propose others):\n"
        f"{hints_block}\n"
        f"\n"
        f"Captured content:\n"
        f"- Title: {extracted.title or '(none)'}\n"
        f"- Author: {extracted.author or '(unknown)'}\n"
        f"- Media kind: {extracted.media_kind.value}\n"
        f"\n"
        f"Body (truncated to first 8000 chars):\n"
        f"\n"
        f"{body_excerpt}\n"
    )


async def classify(
    *,
    extracted: Extracted,
    platform: Platform,
    sibling_topics: list[str],
    topic_hints: list[str],
) -> ClassificationResult:
    """Single Anthropic call → ClassificationResult."""
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    user_msg = build_user_message(
        extracted=extracted,
        platform=platform,
        sibling_topics=sibling_topics,
        topic_hints=topic_hints,
    )

    response = await client.messages.create(
        model=settings.classifier_model,
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_msg}],
    )

    text = response.content[0].text.strip()
    # Strip optional code-fence if the model wrapped output.
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    payload: dict[str, Any] = json.loads(text)

    return ClassificationResult.model_validate(payload)
