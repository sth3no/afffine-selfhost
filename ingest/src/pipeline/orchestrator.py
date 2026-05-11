"""Per-row pipeline orchestrator.

State machine: extracting → classifying → filing → done.
Per-step idempotency: skips work whose result is already persisted on
the row (e.g., classifier_topic populated → don't re-classify).

Exceptions propagate to the worker, which calls repo.mark_failed with
the appropriate backoff. The orchestrator never calls mark_failed
itself — separation of concerns.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from src.config import Platform, TopicsConfig, settings
from src.db import CaptureRepository, CaptureRow
from src.pipeline.classification import ClassificationResult
from src.pipeline.extracted import Extracted
from src.pipeline.filer import Filer
from src.pipeline.markdown_render import count_keyframe_refs, markdown_to_blocks
from src.pipeline.chunked_render import chunked_render
from src.pipeline.templated_render import TemplatedOutput, fallback_title, render as templated_render
from src.pipeline.template_synth import synthesize_template
from src.pipeline.templates import TemplatesRepository

log = logging.getLogger(__name__)

ExtractFunc = Callable[[str, Platform], Awaitable[Extracted]]
ClassifyFunc = Callable[..., Awaitable[ClassificationResult]]  # kwargs-style
RenderFunc = Callable[..., Awaitable[TemplatedOutput]]
SynthFunc = Callable[..., Awaitable[Any]]  # returns ContentTemplate


async def process_capture(
    row: CaptureRow,
    *,
    platform: Platform,
    topics: TopicsConfig,
    repo: CaptureRepository,
    filer: Filer,
    extract_fn: ExtractFunc,
    classify_fn: ClassifyFunc,
    templates_repo: TemplatesRepository,
    render_fn: RenderFunc | None = None,
    synth_fn: SynthFunc | None = None,
) -> None:
    """Run the full pipeline for one capture row.

    Pre-conditions: row.status == 'extracting' (already claimed by the worker).
    Post-conditions: row.status == 'done' (success), or exception propagated
    to caller (failure → caller responsible for mark_failed).
    """
    render_fn = render_fn or templated_render
    synth_fn = synth_fn or synthesize_template

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

    # ── Resolve or synthesize template ─────────────────────────────────
    template = await templates_repo.resolve(
        platform_id=platform.id, topic=result.topic or "*",
    )
    if template is None:
        try:
            template = await synth_fn(
                platform_id=platform.id,
                topic=result.topic or "*",
                sample_extracted=extracted,
                templates_repo=templates_repo,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("template synthesis failed; falling back to (*, *): %s", e)
            template = await templates_repo.resolve(platform_id="*", topic="*")
            if template is None:
                raise RuntimeError(
                    "No (*, *) seed template exists and synthesis failed — "
                    "cannot render this capture."
                ) from e

    # ── Snapshot inputs for replay ─────────────────────────────────────
    await repo.save_extracted_snapshot(
        capture_id=row.id,
        snapshot=_extracted_to_dict(extracted),
    )

    # ── Templated render ───────────────────────────────────────────────
    # For long transcripts we route through chunked_render (map-reduce
    # over chunks of the body) instead of the single-call render_fn.
    # The reducer sees the full picture of the source via the chunk
    # digest, which is what a clickbait-resolving lede needs.
    keyframes = (extracted.extra or {}).get("keyframes") or []
    body_len = len(extracted.body_md or "")
    use_chunked = body_len > settings.chunked_render_threshold_chars
    try:
        if use_chunked:
            log.info(
                "transition",
                extra={"step": "render_chunked", "body_chars": body_len},
            )
            rendered = await chunked_render(
                template=template, extracted=extracted, keyframes=keyframes,
            )
        else:
            rendered = await render_fn(
                template=template, extracted=extracted, keyframes=keyframes,
            )
    except Exception as e:  # noqa: BLE001
        log.warning("templated render failed: %s", e)
        rendered = None

    if rendered is not None:
        new_title = (rendered.title or "").strip() or fallback_title(extracted, url=row.url)
        await repo.save_template_run(
            capture_id=row.id,
            template_id=template.id,
            prompt_used=template.system_prompt,
            output_raw=rendered.body_md,
        )
    else:
        new_title = fallback_title(extracted, url=row.url)

    try:
        await filer._mcp.set_doc_title(row.doc_id, new_title)
        log.info("transition", extra={"step": "titled", "title": new_title})
    except Exception as e:  # noqa: BLE001
        log.warning("set_doc_title failed (continuing): %s", e)

    # ── File (move + append body) ──────────────────────────────────────
    platform_path = ["Sources", platform.group, platform.folder_name]
    folder_id = await filer.move_to_topic_folder(
        platform_path=platform_path, result=result,
    )

    if folder_id is not None:
        topic_path = "/".join(platform_path + [result.topic or ""])
    else:
        topic_path = "/".join(platform_path)

    await repo.mark_filing(capture_id=row.id, topic_path=topic_path)
    log.info("transition", extra={"step": "filed", "topic_path": topic_path})

    if folder_id is not None:
        await filer._mcp.move_document(row.doc_id, folder_id=folder_id)

    # ── Render the doc body via the rich block emitter ─────────────────
    await _replace_doc_body_templated(
        filer=filer,
        doc_id=row.doc_id,
        rendered=rendered,
        keyframes=keyframes,
        url=row.url,
        extracted=extracted,
    )

    await repo.mark_done(row.id)
    log.info("transition", extra={"step": "done"})


def _extracted_to_dict(extracted: Extracted) -> dict:
    """Serialize an Extracted record to a JSON-able dict for snapshotting.

    `url` is intentionally omitted — it lives on the parent capture row,
    not on Extracted, so the rerender endpoint reads it from row.url.
    """
    return {
        "title": extracted.title,
        "body_md": extracted.body_md,
        "author": extracted.author,
        "published_at": extracted.published_at.isoformat() if extracted.published_at else None,
        "media_kind": extracted.media_kind.value,
        "extra": extracted.extra,
    }


async def _replace_doc_body_templated(
    *,
    filer: Filer,
    doc_id: str,
    rendered: TemplatedOutput | None,
    keyframes: list[dict[str, Any]],
    url: str | None,
    extracted: Extracted | None = None,
) -> None:
    """Delete the stub block and append the templated layout:
        [embed url]
        [callout: lede]           (when rendered.lede is non-empty)
        ## Summary
        - bullets
        <body_md tree>             ← template's structured analysis
        ## Keyframes              (when body_md referenced zero kf:N refs
                                    AND keyframes are available — fallback)
        <image blocks>             ← one per keyframe
        ## Transcript             (when extracted.body_md is non-empty)
        <extracted.body_md tree>
        Source: <url>

    The transcript appendix is the user's primary signal — LLM summaries
    are useful but the raw source must be preserved so nothing is lost.
    """
    try:
        await _delete_stub_block(filer=filer, doc_id=doc_id)
    except Exception as e:  # noqa: BLE001
        log.warning("stub block cleanup failed (continuing): %s", e)

    blocks: list[dict[str, Any]] = []
    if url:
        blocks.append(url_embed_block(url))

    if rendered is None:
        # Render failed (no API key / Claude error / parse fail). The capture
        # still completes (worker mark_done is called by the caller), but
        # surface the degraded state so the user knows the doc isn't fully
        # processed.
        blocks.append({
            "type": "callout",
            "text": "Render failed — see server logs. Use POST /captures/{id}/rerender to retry.",
        })

    if rendered is not None:
        if rendered.lede and rendered.lede.strip():
            blocks.append({"type": "callout", "text": rendered.lede.strip()})
        if rendered.summary_md:
            blocks.append({"type": "paragraph", "style": "h2", "text": "Summary"})
            blocks.extend(
                await markdown_to_blocks(rendered.summary_md, keyframes=keyframes, mcp_client=filer._mcp)
            )
        if rendered.body_md:
            blocks.extend(
                await markdown_to_blocks(rendered.body_md, keyframes=keyframes, mcp_client=filer._mcp)
            )

    # Phase 15 fallback: when keyframes are available but body_md referenced
    # zero of them, surface them as a `## Keyframes` appendix so the
    # vision-call cost wasn't wasted. Templates that DO reference keyframes
    # inline via `kf:N` skip this fallback.
    if (
        rendered is not None
        and rendered.body_md
        and keyframes
        and not count_keyframe_refs(rendered.body_md)
    ):
        blocks.append({"type": "paragraph", "style": "h2", "text": "Keyframes"})
        for kf in keyframes:
            source_id = kf.get("blob_source_id")
            if not source_id:
                continue
            blocks.append({
                "type": "image",
                "sourceId": source_id,
                "caption": kf.get("caption") or "",
            })

    # Always append the raw transcript/body extracted from the source.
    # The template's body_md is a summary view; this is the verbatim source
    # so detail/timestamps/citations are never lost to LLM compression.
    if extracted is not None and extracted.body_md and extracted.body_md.strip():
        transcript_md = strip_extractor_metadata(extracted.body_md)
        if transcript_md.strip():
            blocks.append({"type": "paragraph", "style": "h2", "text": "Transcript"})
            blocks.extend(
                await markdown_to_blocks(transcript_md, keyframes=keyframes, mcp_client=filer._mcp)
            )

    if url:
        blocks.append({
            "type": "paragraph",
            "style": "text",
            "text": [{"text": "Source: "}, {"text": url, "italic": True, "link": url}],
        })

    if not blocks:
        blocks.append({"type": "paragraph", "style": "text", "text": "(no rendered content)"})

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


def strip_extractor_metadata(body_md: str) -> str:
    """Strip the leading bold-title / `_by author_` / `Source:` lines AND the
    inner `## Transcript` heading that the cobalt extractor (and similar)
    prepends to body_md.

    The orchestrator wraps body_md in its own `## Transcript` section + adds
    a `Source: <url>` footer block, so the duplicates that come from the
    extractor are visual noise. This helper strips them.

    Returns the body_md with leading prefix lines removed. If body_md has
    no recognizable prefix, returns the input unchanged.
    """
    lines = body_md.splitlines()
    out_start = 0
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped:
            # Blank line within the prefix block — keep skipping until
            # we hit a non-prefix line.
            continue
        if (
            (stripped.startswith("**") and stripped.endswith("**"))
            or (stripped.startswith("_") and stripped.endswith("_"))
            or stripped.lower().startswith("source:")
            or stripped.lower().startswith("## transcript")
        ):
            out_start = i + 1
            continue
        # First real content line — stop stripping.
        break
    return "\n".join(lines[out_start:]).lstrip("\n")


def url_embed_block(url: str) -> dict[str, Any]:
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
