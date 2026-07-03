"""Map-reduce rendering for long transcripts.

When `extracted.body_md` exceeds `settings.chunked_render_threshold_chars`
the single-call render path is bypassed in favour of:

1. **Split** the body into N chunks (~`chunk_size_chars` each with
   `chunk_overlap_chars` overlap). When the body contains YouTube-style
   `[hh:mm:ss](https://...)` timestamp anchors, we prefer to split on
   timestamp boundaries so each chunk is a coherent N-minute window.
2. **Map** each chunk → `ChunkSummary` via a Sonnet call. Cheap per call
   because the chunk is small; the prompt is identical for every chunk so
   prompt caching applies after the first.
3. **Reduce** all `ChunkSummary` records + metadata + keyframes → final
   `TemplatedOutput` via one more Sonnet call that now has the FULL
   picture of the source (not just the first 12k chars).

This is the right shape for clickbait-resolving ledes: the reveal often
lives in the final third of the video, which the single-call truncated
path never saw.

Cost shape:
  - Single-call path:   1 Sonnet call  (existing `templated_render.render`)
  - Map-reduce path:    N+1 Sonnet calls for an N-chunk transcript
  - Capped by `max_chunks_per_capture` to bound worst-case cost.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field

from src.config import settings
from src.llm_clients import anthropic_client
from src.llm_usage import record_anthropic_usage
from src.pipeline.extracted import Extracted
from src.pipeline.templates import ContentTemplate
from src.pipeline.templated_render import TemplatedOutput

log = logging.getLogger(__name__)


# ── ChunkSummary: the map step's output ─────────────────────────────


class ChunkSummary(BaseModel):
    """One chunk of the source's compressed view. Many of these are
    concatenated and fed to the reducer."""

    section_title: str = Field(
        description="Short (2-6 word) label describing what this chunk is "
                    "about. Used as a `## Section title` heading in body_md."
    )
    timestamp_range: str | None = Field(
        default=None,
        description="Inclusive range like '0:00–4:30' when the chunk's "
                    "boundaries are derivable from timestamp anchors. "
                    "Null for non-timestamped content (articles)."
    )
    key_points: list[str] = Field(
        description="3-6 bullet points capturing the chunk's main claims, "
                    "data, or insights. Each is one short factual line."
    )
    notable_quotes: list[str] = Field(
        default_factory=list,
        description="0-2 verbatim quotes worth preserving (max 200 chars each). "
                    "Only when a direct quote is more striking than a paraphrase."
    )
    references: list[str] = Field(
        default_factory=list,
        description="URLs, citations, paper names, or related works "
                    "mentioned in this chunk. Strips sponsor/affiliate noise."
    )
    reveal: str | None = Field(
        default=None,
        description="If THIS chunk contains the answer to a clickbait/teaser "
                    "in the source title, write one sentence answering it. "
                    "Otherwise null."
    )


# ── Chunker ──────────────────────────────────────────────────────────


# YouTube transcript timestamp anchors look like `[**0:00**](https://...)` or
# `[0:42](...)`. The cobalt extractor emits this format.
_TIMESTAMP_LINE_RE = re.compile(
    r"^\s*\[\*?\*?(?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\*?\*?\]\([^)]+\)",
    re.MULTILINE,
)


def split_into_chunks(
    body_md: str,
    *,
    chunk_size: int,
    overlap: int,
    max_chunks: int,
) -> list[dict[str, Any]]:
    """Return a list of `{text, timestamp_range}` dicts.

    Splitting strategy:
      1. Find timestamp anchors in the body.
      2. If we find at least 3 anchors AND the body has > chunk_size chars,
         build chunks by grouping anchors so each chunk's content is
         roughly chunk_size chars. Set `timestamp_range` from the first
         and last anchor in the chunk.
      3. Otherwise (no anchors, or short body), fall back to fixed-size
         splits with `overlap` chars of overlap between adjacent chunks.

    The output is capped at `max_chunks` entries. When the cap binds,
    the last chunk's `text` is the truncated tail with a note.
    """
    if not body_md or len(body_md) <= chunk_size:
        # Body fits in one chunk — caller's threshold check should have
        # routed this past the chunker, but be defensive.
        return [{"text": body_md, "timestamp_range": None}]

    anchors = list(_TIMESTAMP_LINE_RE.finditer(body_md))

    if len(anchors) >= 3:
        return _split_on_timestamps(body_md, anchors, chunk_size, max_chunks)
    return _split_fixed_size(body_md, chunk_size, overlap, max_chunks)


def _split_on_timestamps(
    body_md: str,
    anchors: list[re.Match[str]],
    chunk_size: int,
    max_chunks: int,
) -> list[dict[str, Any]]:
    """Group timestamp anchors so each chunk is ~chunk_size chars. The
    chunk boundary falls at the next anchor that pushes the chunk past
    chunk_size."""
    chunks: list[dict[str, Any]] = []
    current_start = 0
    current_first_ts: str | None = None
    current_last_ts: str | None = None

    for i, m in enumerate(anchors):
        if current_first_ts is None:
            current_first_ts = m.group("ts")

        # Distance from current chunk start to this anchor.
        char_offset = m.start() - current_start

        # If adding this anchor would exceed chunk_size AND we already
        # have at least one anchor in the current chunk, close it.
        if char_offset >= chunk_size and current_last_ts is not None:
            chunk_text = body_md[current_start:m.start()].strip()
            if chunk_text:
                chunks.append({
                    "text": chunk_text,
                    "timestamp_range": _ts_range(current_first_ts, current_last_ts),
                })
            current_start = m.start()
            current_first_ts = m.group("ts")
            current_last_ts = m.group("ts")
        else:
            current_last_ts = m.group("ts")

        if len(chunks) >= max_chunks - 1:
            break

    # Final tail chunk.
    tail = body_md[current_start:].strip()
    if tail:
        chunks.append({
            "text": tail,
            "timestamp_range": _ts_range(current_first_ts, current_last_ts),
        })

    # Apply max_chunks cap with a truncation note on the last chunk.
    if len(chunks) > max_chunks:
        truncated = chunks[:max_chunks]
        truncated[-1] = {
            "text": truncated[-1]["text"]
                + "\n\n_(remaining transcript truncated — exceeded max_chunks cap)_",
            "timestamp_range": truncated[-1]["timestamp_range"],
        }
        return truncated
    return chunks


def _ts_range(first: str | None, last: str | None) -> str | None:
    if first is None or last is None:
        return None
    if first == last:
        return first
    return f"{first}–{last}"


def _split_fixed_size(
    body_md: str,
    chunk_size: int,
    overlap: int,
    max_chunks: int,
) -> list[dict[str, Any]]:
    """Plain character-based splitting with overlap. Used for articles
    and other non-timestamped content."""
    chunks: list[dict[str, Any]] = []
    start = 0
    body_len = len(body_md)
    while start < body_len and len(chunks) < max_chunks:
        end = min(start + chunk_size, body_len)
        text = body_md[start:end].strip()
        if text:
            chunks.append({"text": text, "timestamp_range": None})
        if end >= body_len:
            break
        start = end - overlap
    if start < body_len and len(chunks) == max_chunks:
        # Mark truncation on the last chunk.
        chunks[-1] = {
            "text": chunks[-1]["text"]
                + "\n\n_(remaining content truncated — exceeded max_chunks cap)_",
            "timestamp_range": chunks[-1]["timestamp_range"],
        }
    return chunks


# ── Map step: summarize one chunk ────────────────────────────────────


_CHUNK_SYSTEM_PROMPT = """You are summarizing one chunk of a longer source
(video transcript, article, podcast episode) for a personal knowledge base.

You will receive:
- The source's title and author/channel
- The chunk's position (e.g. "chunk 3 of 7")
- The chunk's content (verbatim transcript or article text)
- Optionally: the source's clickbait title — if present, watch for the
  REVEAL in this chunk and surface it in the `reveal` field.

Produce a `ChunkSummary` capturing:
- A short section title (2-6 words) describing the chunk's topic
- The chunk's main key points (3-6 short factual bullets, no fluff)
- 0-2 notable verbatim quotes worth preserving (only when more striking
  than paraphrase)
- Any references / citations / URLs mentioned (strip sponsor/affiliate
  noise — only signal-carrying references)
- If the source title teases something ("which model wins?", "the
  secret to X") AND this chunk contains the answer, write ONE direct
  sentence in `reveal`. Otherwise leave `reveal` null.

Be precise and concise. Do NOT echo teaser phrases like "stay tuned" or
"to be revealed" — if the answer isn't here, leave `reveal` null and
let the next chunk find it.

Return strict JSON matching the ChunkSummary schema only.
"""


async def _summarize_chunk(
    *,
    client: AsyncAnthropic,
    chunk_text: str,
    chunk_index: int,
    total_chunks: int,
    timestamp_range: str | None,
    source_title: str | None,
    source_author: str | None,
) -> ChunkSummary:
    """One Sonnet call per chunk. The system prompt is identical for every
    chunk in a capture so prompt caching hits after the first."""
    user_msg = (
        f"Source: {source_title or '(none)'} — {source_author or '(unknown)'}\n"
        f"Chunk {chunk_index + 1} of {total_chunks}"
        + (f" (timestamps {timestamp_range})\n" if timestamp_range else "\n")
        + "\n"
        + chunk_text
    )
    response = await client.messages.parse(
        model=settings.summarizer_model,
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": _CHUNK_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_msg}],
        output_format=ChunkSummary,
    )
    record_anthropic_usage(response, kind="render_map", model=settings.summarizer_model)
    if response.parsed_output is None:
        raise RuntimeError(
            f"chunked_render: chunk {chunk_index} parsed_output is None"
        )
    return response.parsed_output


# ── Reduce step: chunk summaries → TemplatedOutput ───────────────────


def _format_chunk_summaries(chunks: list[ChunkSummary]) -> str:
    """Serialize chunk summaries into a structured block that the reducer
    reads as the source content."""
    parts: list[str] = []
    for i, c in enumerate(chunks):
        header = f"### Section {i + 1}: {c.section_title}"
        if c.timestamp_range:
            header += f" [{c.timestamp_range}]"
        parts.append(header)
        if c.reveal:
            parts.append(f"REVEAL_FOUND: {c.reveal}")
        parts.append("Key points:")
        for kp in c.key_points:
            parts.append(f"- {kp}")
        if c.notable_quotes:
            parts.append("Notable quotes:")
            for q in c.notable_quotes:
                parts.append(f'> "{q}"')
        if c.references:
            parts.append("References:")
            for r in c.references:
                parts.append(f"- {r}")
        parts.append("")
    return "\n".join(parts)


async def _reduce_to_templated_output(
    *,
    client: AsyncAnthropic,
    template: ContentTemplate,
    extracted: Extracted,
    keyframes: list[dict[str, Any]],
    chunk_summaries: list[ChunkSummary],
) -> TemplatedOutput:
    """Final Sonnet call: takes all chunk summaries + metadata + keyframes
    and produces the TemplatedOutput. Uses the template's system_prompt
    verbatim — the same prompt the single-call path would use — but the
    user-message context is the chunk-summary digest, not the raw transcript."""
    description = (extracted.extra or {}).get("description")
    video_summary = (extracted.extra or {}).get("video_summary")

    parts: list[str] = [
        "This is a LONG capture — the source has been pre-summarized by a "
        "map step. The transcript was split into chunks, each summarized "
        "separately. Your job is the REDUCE step: synthesize all chunk "
        "summaries into ONE TemplatedOutput.",
        "",
        f"Source metadata:",
        f"- Title: {extracted.title or '(none)'}",
        f"- Author/channel: {extracted.author or '(unknown)'}",
        f"- Media kind: {extracted.media_kind.value}",
        "",
    ]

    # Any chunk whose `reveal` field is populated answers a clickbait
    # teaser. Surface these at the top so the lede resolver sees them.
    reveals = [c.reveal for c in chunk_summaries if c.reveal]
    if reveals:
        parts.append("REVEALS found by map step (use to populate `lede`):")
        for r in reveals:
            parts.append(f"- {r}")
        parts.append("")

    if description:
        parts.append(
            "Source description (from publisher — mine for citations / "
            "chapter markers / related links):"
        )
        parts.append(str(description))
        parts.append("")

    if video_summary:
        parts.append("Vision-grounded summary (from Phase 13 video analysis):")
        parts.append(str(video_summary))
        parts.append("")

    if keyframes:
        parts.append(
            "Available keyframes (reference inline via `![caption](kf:<n>)`):"
        )
        for i, kf in enumerate(keyframes):
            ts = kf.get("timestamp_seconds", 0.0)
            caption = (kf.get("caption") or "").strip()
            parts.append(f"  [{i}] t={ts:.1f}s — {caption}")
        parts.append("")

    parts.append(f"Chunk-summary digest ({len(chunk_summaries)} chunks):")
    parts.append("")
    parts.append(_format_chunk_summaries(chunk_summaries))

    user_msg = "\n".join(parts)

    response = await client.messages.parse(
        model=settings.summarizer_model,
        max_tokens=4096,
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
    record_anthropic_usage(response, kind="render_reduce", model=settings.summarizer_model)
    if response.parsed_output is None:
        raise RuntimeError(
            "chunked_render: reduce parsed_output is None"
        )
    return response.parsed_output


# ── Public: chunked_render() ─────────────────────────────────────────


async def chunked_render(
    *,
    template: ContentTemplate,
    extracted: Extracted,
    keyframes: list[dict[str, Any]],
) -> TemplatedOutput:
    """Map-reduce render for long transcripts.

    Splits `extracted.body_md` into chunks, summarizes each in parallel,
    then reduces the chunk summaries into a single `TemplatedOutput`
    using the template's system_prompt.

    Cost: N+1 Sonnet calls where N is the number of chunks (capped at
    `settings.max_chunks_per_capture`).
    """
    chunks = split_into_chunks(
        extracted.body_md or "",
        chunk_size=settings.chunk_size_chars,
        overlap=settings.chunk_overlap_chars,
        max_chunks=settings.max_chunks_per_capture,
    )
    if not chunks:
        # Defensive: should never happen given the orchestrator's
        # threshold gating, but if it does, fall back to an empty digest
        # so the reducer at least produces a TemplatedOutput.
        chunks = [{"text": "(empty source)", "timestamp_range": None}]

    log.info(
        "chunked_render: split into %d chunks (body=%d chars)",
        len(chunks),
        len(extracted.body_md or ""),
    )

    client = anthropic_client()

    # Map step — run all chunks in parallel. Anthropic SDK handles
    # connection pooling under the hood; concurrent calls are fine.
    chunk_summaries = await asyncio.gather(*[
        _summarize_chunk(
            client=client,
            chunk_text=chunk["text"],
            chunk_index=i,
            total_chunks=len(chunks),
            timestamp_range=chunk["timestamp_range"],
            source_title=extracted.title,
            source_author=extracted.author,
        )
        for i, chunk in enumerate(chunks)
    ])

    log.info("chunked_render: %d chunk summaries ready, reducing…",
             len(chunk_summaries))

    # Reduce step — one final Sonnet call with the chunk digest.
    return await _reduce_to_templated_output(
        client=client,
        template=template,
        extracted=extracted,
        keyframes=keyframes,
        chunk_summaries=chunk_summaries,
    )
