"""Per-row pipeline orchestrator.

State machine: extracting → summarizing → classifying → filing → done.
Per-step idempotency: skips work whose result is already persisted on
the row (e.g., classifier_topic populated → don't re-classify).

Exceptions propagate to the worker, which calls repo.mark_failed with
the appropriate backoff. The orchestrator never calls mark_failed
itself — separation of concerns.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from src.config import Platform, TopicsConfig, settings
from src.db import CaptureRepository, CaptureRow
from src.pipeline.classification import ClassificationResult
from src.pipeline.extracted import Extracted
from src.pipeline.filer import Filer
from src.pipeline.summarizer import SummaryResult, fallback_title, summarize

log = logging.getLogger(__name__)

ExtractFunc = Callable[[str, Platform], Awaitable[Extracted]]
ClassifyFunc = Callable[..., Awaitable[ClassificationResult]]  # kwargs-style
SummarizeFunc = Callable[[Extracted], Awaitable[SummaryResult]]


async def process_capture(
    row: CaptureRow,
    *,
    platform: Platform,
    topics: TopicsConfig,
    repo: CaptureRepository,
    filer: Filer,
    extract_fn: ExtractFunc,
    classify_fn: ClassifyFunc,
    summarize_fn: SummarizeFunc | None = None,
) -> None:
    """Run the full pipeline for one capture row.

    Pre-conditions: row.status == 'extracting' (already claimed by the worker).
    Post-conditions: row.status == 'done' (success), or exception propagated
    to caller (failure → caller responsible for mark_failed).
    """
    summarize_fn = summarize_fn or summarize

    if not row.url:
        # Phase 3 supports text-only captures (shared_text). Phase 6's
        # extractors all expect URLs; text-only goes straight to classifying
        # using shared_text as the body.
        extracted = Extracted(
            title=row.shared_title,
            body_md=row.shared_text or "",
            author=None,
            published_at=None,
            media_kind=_media_kind_for_text(),
            extra={"text_only": True},
        )
    else:
        # Phase 13: pass mcp_client + capture_id so extractors that
        # support video frame analysis (cobalt_ext) can upload keyframe
        # blobs. All extractors swallow unknown kwargs via **_kwargs.
        extracted = await extract_fn(
            row.url,
            platform,
            mcp_client=filer._mcp,
            capture_id=row.id,
        )

    log.info("transition", extra={"step": "extracted", "platform": platform.id})

    # ── Summarize (best-effort) → new doc title + summary block ──────
    # Phase 13: prefer the vision-grounded summary from video_analysis
    # over a re-run of the text-only summarizer. Title still comes from
    # the summarizer (cheap Haiku call) — vision focuses on summary
    # content, not naming.
    video_summary = (extracted.extra or {}).get("video_summary")
    summary = await _try_summarize(extracted, summarize_fn=summarize_fn)
    new_title = (summary.title if summary else fallback_title(extracted, url=row.url)).strip() or "Untitled capture"
    summary_md = video_summary or (summary.summary_md if summary else None)

    try:
        await filer._mcp.set_doc_title(row.doc_id, new_title)
        log.info("transition", extra={"step": "titled", "title": new_title})
    except Exception as e:  # noqa: BLE001 — title rename failures shouldn't kill the pipeline
        log.warning("set_doc_title failed (continuing): %s", e)

    # ── Classify (or reuse cached classifier output on retry) ────────
    if row.classifier_topic is not None or row.classifier_conf is not None:
        result = ClassificationResult(
            topic=row.classifier_topic,
            confidence=float(row.classifier_conf or 0.0),
            reasoning=row.classifier_reasoning or "(reused from prior attempt)",
        )
    else:
        sibling_topics = _list_existing_siblings(filer, platform)
        topic_hints = topics.topic_hints.get(platform.id, [])
        result = await classify_fn(
            extracted=extracted,
            platform=platform,
            sibling_topics=await sibling_topics,
            topic_hints=topic_hints,
        )
        await repo.mark_classifying(
            capture_id=row.id,
            topic=result.topic,
            confidence=result.confidence,
            reasoning=result.reasoning,
        )

    log.info("transition", extra={"step": "classified", "topic": result.topic, "confidence": result.confidence})

    # ── File (move + append body) ────────────────────────────────────
    platform_path = ["Sources", platform.group, platform.folder_name]
    folder_id = await filer.move_to_topic_folder(platform_path=platform_path, result=result)

    if folder_id is not None:
        topic_path = "/".join(platform_path + [result.topic or ""])
    else:
        topic_path = "/".join(platform_path)

    await repo.mark_filing(capture_id=row.id, topic_path=topic_path)

    log.info("transition", extra={"step": "filed", "topic_path": topic_path})

    if folder_id is not None:
        await filer._mcp.move_document(row.doc_id, folder_id=folder_id)

    # Replace stub doc body with proper structured blocks.
    await _replace_doc_body(
        filer=filer,
        doc_id=row.doc_id,
        extracted=extracted,
        summary_md=summary_md,
        url=row.url,
    )

    # ── Done ─────────────────────────────────────────────────────────
    await repo.mark_done(row.id)
    log.info("transition", extra={"step": "done"})


async def _try_summarize(
    extracted: Extracted,
    *,
    summarize_fn: SummarizeFunc,
) -> SummaryResult | None:
    """Best-effort summarization. Returns None on any failure (no API key,
    timeout, parse error) — caller falls back to a deterministic title and
    skips the summary block."""
    if not settings.anthropic_api_key:
        return None
    if not (extracted.body_md or "").strip() and not extracted.title:
        return None
    try:
        return await summarize_fn(extracted)
    except Exception as e:  # noqa: BLE001
        log.warning("summarizer failed (continuing without summary): %s", e)
        return None


async def _replace_doc_body(
    *,
    filer: Filer,
    doc_id: str,
    extracted: Extracted,
    summary_md: str | None,
    url: str | None,
) -> None:
    """Delete the stub `> Capturing...` block and replace with structured content.

    Stub block created by api.py is the only paragraph at the time the
    orchestrator runs (the doc was just created with that one block). We
    look it up via list_doc_blocks and delete it; if anything else has
    appeared (manual edit, race), we leave it alone and just append.
    """
    try:
        await _delete_stub_block(filer=filer, doc_id=doc_id)
    except Exception as e:  # noqa: BLE001
        log.warning("stub block cleanup failed (continuing): %s", e)

    blocks = _build_body_blocks(extracted=extracted, summary_md=summary_md, url=url)
    await filer._mcp.append_blocks(doc_id, blocks)


async def _delete_stub_block(*, filer: Filer, doc_id: str) -> None:
    listing = await filer._mcp.list_doc_blocks(doc_id)
    items = listing.get("blocks", []) if isinstance(listing, dict) else []
    for block in items:
        flavour = block.get("flavour", "")
        text = (block.get("text") or "").strip()
        if flavour == "affine:paragraph" and text.startswith("> Capturing..."):
            await filer._mcp.delete_block(doc_id, block.get("id"))
            return


def _url_embed_block(url: str) -> dict[str, Any]:
    """Pick the right AFFiNE embed block type for a given URL.

    AFFiNE has dedicated rich-preview embed flavours for a few platforms
    (YouTube renders an inline player thumbnail, GitHub renders a repo
    card, etc.). For everything else, `bookmark` produces a generic URL
    card with og:image / og:title / og:description fetched by AFFiNE
    server-side after the block lands.

    | Host                 | Block            |
    |----------------------|------------------|
    | youtube.com / youtu.be | embed-youtube  |
    | github.com           | embed-github     |
    | figma.com            | embed-figma      |
    | loom.com             | embed-loom       |
    | (anything else)      | bookmark         |
    """
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()

    if host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com"):
        return {"type": "embed-youtube", "url": url}
    if host == "github.com" or host.endswith(".github.com"):
        return {"type": "embed-github", "url": url}
    if host == "figma.com" or host.endswith(".figma.com"):
        return {"type": "embed-figma", "url": url}
    if host == "loom.com" or host.endswith(".loom.com"):
        return {"type": "embed-loom", "url": url}
    return {"type": "bookmark", "url": url}


def _build_body_blocks(
    *,
    extracted: Extracted,
    summary_md: str | None,
    url: str | None,
) -> list[dict[str, Any]]:
    """Compose structured block specs for the doc body.

    Layout:
      [embed url]          (rich preview at the very top — youtube/github/etc;
                            falls back to bookmark for unknown hosts)
      ## Summary           (if summary available)
      <summary paragraph>
      ## Keyframes         (Phase 13 — if video analysis produced any)
      [image] [caption]    (one image block + caption paragraph per keyframe)
      ## Description       (if extractor provided one)
      <description paragraph>
      ## Transcript / body
      <body paragraphs>
      Source: <url>        (italic at bottom)
    """
    blocks: list[dict[str, Any]] = []

    # Rich URL preview at the very top of the doc — gives the reader a
    # thumbnail / "Watch on YouTube" affordance before scrolling through
    # the summary + transcript.
    if url:
        blocks.append(_url_embed_block(url))

    if summary_md:
        blocks.append({"type": "paragraph", "style": "h2", "text": "Summary"})
        # Run the summary through _markdown_to_blocks so bulleted summaries
        # (the new prompt format — "- item" per line) render as proper
        # AFFiNE list blocks rather than a single paragraph with literal
        # dashes. Single-paragraph summaries collapse to one paragraph
        # block as before.
        blocks.extend(_markdown_to_blocks(summary_md.strip()))

    # Phase 13: keyframes from video analysis (if any).
    keyframes = (extracted.extra or {}).get("keyframes") or []
    if keyframes:
        blocks.append({"type": "paragraph", "style": "h2", "text": "Keyframes"})
        for kf in keyframes:
            source_id = kf.get("blob_source_id")
            caption = (kf.get("caption") or "").strip()
            ts = kf.get("timestamp_seconds", 0.0)
            if not source_id:
                continue
            blocks.append({"type": "image", "sourceId": source_id, "caption": caption})
            # Caption + timestamp as a small italic paragraph below the image
            ts_label = f"[{ts:.1f}s]"
            blocks.append({
                "type": "paragraph",
                "style": "text",
                "text": [
                    {"text": ts_label, "italic": True},
                    {"text": " "},
                    {"text": caption} if caption else {"text": ""},
                ],
            })

    description = (extracted.extra or {}).get("description")
    body_md = extracted.body_md or ""

    if description:
        blocks.append({"type": "paragraph", "style": "h2", "text": "Description"})
        # Split on blank lines so each paragraph in the source description
        # becomes its own block — matches AFFiNE's reading rhythm and looks
        # like the rest of the doc body. Single-paragraph descriptions
        # collapse to a single block automatically.
        for chunk in re.split(r"\n\s*\n", str(description).strip()):
            chunk = chunk.strip()
            if chunk:
                blocks.append({"type": "paragraph", "style": "text", "text": chunk})

    # Body parsing: split markdown headings into proper heading blocks; keep
    # everything else as paragraphs separated by blank lines. This handles
    # the cobalt extractor's "## Transcript (Whisper via cobalt)" cleanly.
    body_blocks = _markdown_to_blocks(body_md, skip_top_metadata=summary_md is not None or bool(description))
    blocks.extend(body_blocks)

    if url:
        blocks.append({
            "type": "paragraph",
            "style": "text",
            "text": [{"text": "Source: "}, {"text": url, "italic": True, "link": url}],
        })

    if not blocks:
        blocks.append({"type": "paragraph", "style": "text", "text": "(no extracted content)"})
    return blocks


_HEADING_PREFIXES = (
    ("###### ", "h6"),
    ("##### ", "h5"),
    ("#### ", "h4"),
    ("### ", "h3"),
    ("## ", "h2"),
    ("# ", "h1"),
)


# Inline markdown link with optional bold/italic on the label:
#   [text](url)
#   [**bold**](url)
#   [*italic*](url)
# Captures: opener (** or * or empty), label, closer (** or * or empty), url.
_INLINE_LINK_RE = re.compile(
    r"\[(?P<open>\*\*|\*)?(?P<label>[^\]]+?)(?P<close>\*\*|\*)?\]\((?P<url>[^)\s]+)\)"
)


def _parse_inline_markdown(text: str):
    """Parse `[**0:00**](https://...)` and similar inline links into the
    InlineOp[] structure that the mcp-ext block-builder turns into rich-text
    deltas. Plain text without inline syntax falls through unchanged so we
    don't pay parser cost for normal paragraphs.
    """
    if "](" not in text:
        return text  # fast path: no possible link
    parts: list[dict] = []
    pos = 0
    for m in _INLINE_LINK_RE.finditer(text):
        if m.start() > pos:
            parts.append({"text": text[pos:m.start()]})
        opener = m.group("open") or ""
        closer = m.group("close") or ""
        label = m.group("label")
        url = m.group("url")
        op: dict = {"text": label, "link": url}
        if opener == "**" and closer == "**":
            op["bold"] = True
        elif opener == "*" and closer == "*":
            op["italic"] = True
        parts.append(op)
        pos = m.end()
    if pos == 0:
        return text  # regex matched nothing usable
    if pos < len(text):
        parts.append({"text": text[pos:]})
    return parts


def _markdown_to_blocks(body_md: str, *, skip_top_metadata: bool = False) -> list[dict[str, Any]]:
    """Lightweight markdown → block-spec converter.

    - Lines starting with `# `…`###### ` become heading blocks.
    - Blank line breaks paragraphs.
    - Non-heading content collects into paragraph blocks.
    - When skip_top_metadata=True, drops leading bold-title / by-author /
      Source: lines emitted by cobalt_ext (those are now rendered via the
      doc title + the explicit Source link block at the bottom).
    """
    blocks: list[dict[str, Any]] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if not paragraph:
            return
        text = "\n".join(paragraph).strip()
        if text:
            # Parse inline `[label](url)` markdown links so they render as
            # clickable text in AFFiNE — particularly important for the
            # transcript's `[**0:00**](youtube...?t=Ns)` timestamp prefixes.
            blocks.append({
                "type": "paragraph",
                "style": "text",
                "text": _parse_inline_markdown(text),
            })
        paragraph.clear()

    lines = body_md.splitlines()

    if skip_top_metadata:
        # Drop the leading metadata block (bold title, _by_, Source:) until
        # the first heading or blank-then-content boundary.
        keep_from = 0
        for i, raw in enumerate(lines):
            stripped = raw.strip()
            if stripped.startswith("## ") or stripped.startswith("# "):
                keep_from = i
                break
            if stripped.startswith("**") or stripped.startswith("_") or stripped.lower().startswith("source:") or not stripped:
                continue
            keep_from = i
            break
        lines = lines[keep_from:]

    for raw in lines:
        stripped = raw.lstrip()
        heading_style: str | None = None
        for prefix, style in _HEADING_PREFIXES:
            if stripped.startswith(prefix):
                heading_style = style
                stripped = stripped[len(prefix):].strip()
                break

        if heading_style is not None:
            flush_paragraph()
            blocks.append({"type": "paragraph", "style": heading_style, "text": stripped})
        elif stripped.startswith("- ") or stripped.startswith("* "):
            # Markdown bullet → AFFiNE list block. One block per item; AFFiNE's
            # list flavour represents each item as its own block (consecutive
            # bulleted blocks render as a contiguous list in the editor).
            flush_paragraph()
            item_text = stripped[2:].strip()
            if item_text:
                blocks.append({
                    "type": "list",
                    "style": "bulleted",
                    "text": _parse_inline_markdown(item_text),
                })
        elif raw.strip() == "":
            flush_paragraph()
        else:
            paragraph.append(raw)

    flush_paragraph()
    return blocks


async def _list_existing_siblings(filer: Filer, platform: Platform) -> list[str]:
    """Return immediate child folder names under Sources/<group>/<platform>/."""
    tree = await filer._mcp.list_folder_tree()
    siblings = tree.get("tree", [])
    for segment in ("Sources", platform.group, platform.folder_name):
        match = next((n for n in siblings if n.get("name") == segment), None)
        if match is None:
            return []
        siblings = match.get("children", []) or []
    return [s.get("name") for s in siblings if s.get("type") == "folder" and s.get("name")]


def _media_kind_for_text():
    from src.pipeline.extracted import MediaKind
    return MediaKind.TEXT
