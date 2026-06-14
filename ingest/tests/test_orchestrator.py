from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import Platform, TopicsConfig
from src.db import CaptureRow
from src.pipeline.classification import ClassificationResult
from src.pipeline.extracted import Extracted, MediaKind
from src.pipeline.orchestrator import process_capture
from src.pipeline.templated_render import TemplatedOutput


def _row(status="extracting", **kw):
    base = {
        "id": "01J", "url": "https://example.com/article",
        "url_hash": "h", "source_app": None, "shared_title": None, "shared_text": None,
        "platform": "article", "status": status, "doc_id": "d-1", "web_url": "w",
        "topic_path": "Sources/Articles/Web",
    }
    base.update(kw)
    from datetime import datetime, timezone
    base["created_at"] = datetime(2026, 5, 7, tzinfo=timezone.utc)
    return CaptureRow(**base)


def _platform() -> Platform:
    return Platform(id="article", group="Articles", folder_name="Web",
                    hosts=["*"], extractor="markitdown")


def _extracted() -> Extracted:
    return Extracted(
        title="Hello",
        body_md="# Body\n\nContent here.",
        author="someone",
        published_at=None,
        media_kind=MediaKind.TEXT,
        extra={},
    )


def _topics(plat: Platform) -> TopicsConfig:
    return TopicsConfig(platforms=[plat], topic_hints={"article": ["Tech", "Science"]})


def _content_template(**overrides):
    from datetime import datetime, timezone
    from src.pipeline.templates import ContentTemplate
    base = dict(
        id="t_seed", platform_id="*", topic="*",
        name="Default summarizer", system_prompt="prompt",
        status="auto", generator_meta=None, created_by="synth",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return ContentTemplate(**base)


def _fake_templates_repo():
    from src.pipeline.templates import TemplatesRepository
    repo = AsyncMock(spec=TemplatesRepository)
    repo.resolve = AsyncMock(return_value=_content_template())
    return repo


@pytest.fixture
def deps():
    """Shared mock dependency bundle."""
    repo = AsyncMock()
    filer = AsyncMock()
    filer._mcp = AsyncMock()
    filer._mcp.list_folder_tree.return_value = {"totalNodes": 0, "tree": []}
    extract_fn = AsyncMock(return_value=_extracted())
    classify_fn = AsyncMock(return_value=ClassificationResult(
        topic="Tech", confidence=0.92, reasoning="article about tech",
    ))
    return {
        "repo": repo,
        "filer": filer,
        "extract_fn": extract_fn,
        "classify_fn": classify_fn,
        "templates_repo": _fake_templates_repo(),
        "render_fn": AsyncMock(return_value=TemplatedOutput(
            title="Hello", lede=None, summary_md="- a", body_md="content",
        )),
    }


@pytest.mark.asyncio
async def test_process_capture_happy_path_advances_through_all_states(deps):
    plat = _platform()
    deps["filer"].move_to_topic_folder.return_value = "f-tech"
    deps["filer"]._mcp.list_folder_tree.return_value = {"totalNodes": 0, "tree": []}

    await process_capture(
        _row(),
        platform=plat,
        topics=_topics(plat),
        repo=deps["repo"],
        filer=deps["filer"],
        extract_fn=deps["extract_fn"],
        classify_fn=deps["classify_fn"],
        templates_repo=deps["templates_repo"],
        render_fn=deps["render_fn"],
    )

    deps["extract_fn"].assert_awaited_once()
    deps["classify_fn"].assert_awaited_once()
    deps["repo"].mark_classifying.assert_awaited_once()
    deps["repo"].mark_filing.assert_awaited_once()
    deps["repo"].mark_done.assert_awaited_once_with("01J")
    deps["filer"].move_to_topic_folder.assert_awaited_once()
    deps["filer"]._mcp.move_document.assert_awaited_once_with("d-1", folder_id="f-tech")
    deps["filer"]._mcp.append_blocks.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_capture_low_confidence_leaves_at_platform_root(deps):
    """topic=None means filer.move_to_topic_folder returns None → no move_document."""
    plat = _platform()
    deps["classify_fn"].return_value = ClassificationResult(
        topic=None, confidence=0.4, reasoning="ambiguous",
    )
    deps["filer"].move_to_topic_folder.return_value = None

    await process_capture(
        _row(),
        platform=plat,
        topics=_topics(plat),
        repo=deps["repo"],
        filer=deps["filer"],
        extract_fn=deps["extract_fn"],
        classify_fn=deps["classify_fn"],
        templates_repo=deps["templates_repo"],
        render_fn=deps["render_fn"],
    )

    deps["filer"]._mcp.move_document.assert_not_called()
    deps["repo"].mark_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_capture_extractor_failure_calls_mark_failed(deps):
    plat = _platform()
    deps["extract_fn"].side_effect = RuntimeError("yt-dlp bombed")

    with pytest.raises(RuntimeError, match="yt-dlp bombed"):
        await process_capture(
            _row(),
            platform=plat,
            topics=_topics(plat),
            repo=deps["repo"],
            filer=deps["filer"],
            extract_fn=deps["extract_fn"],
            classify_fn=deps["classify_fn"],
            templates_repo=deps["templates_repo"],
            render_fn=deps["render_fn"],
        )

    # Orchestrator re-raises; the worker (Task 3) is responsible for mark_failed
    # with backoff. The orchestrator should NOT have advanced state.
    deps["repo"].mark_done.assert_not_called()


@pytest.mark.asyncio
async def test_process_capture_creates_doc_when_stub_was_deferred(deps):
    """Rows accepted while AFFiNE was down arrive with doc_id=NULL — the
    orchestrator must create the doc first and persist it via repo.set_doc."""
    plat = _platform()
    deps["filer"].move_to_topic_folder.return_value = "f-tech"
    deps["filer"].resolve_or_create_folder.return_value = "f-platform"
    deps["filer"]._mcp.create_doc.return_value = {"docId": "d-late"}

    row = _row(doc_id=None, web_url=None)

    await process_capture(
        row, platform=plat, topics=_topics(plat),
        repo=deps["repo"], filer=deps["filer"],
        extract_fn=deps["extract_fn"], classify_fn=deps["classify_fn"],
        templates_repo=deps["templates_repo"], render_fn=deps["render_fn"],
    )

    deps["filer"]._mcp.create_doc.assert_awaited_once()
    deps["repo"].set_doc.assert_awaited_once()
    set_doc_kwargs = deps["repo"].set_doc.await_args.kwargs
    assert set_doc_kwargs["doc_id"] == "d-late"
    assert "d-late" in set_doc_kwargs["web_url"]
    # The rest of the pipeline ran against the late-created doc.
    deps["repo"].mark_done.assert_awaited_once()
    deps["filer"]._mcp.set_doc_title.assert_awaited_once()
    assert deps["filer"]._mcp.set_doc_title.await_args.args[0] == "d-late"


@pytest.mark.asyncio
async def test_process_capture_reuses_extracted_snapshot_on_retry(deps):
    """A retry of a row whose extraction already succeeded (failure was in a
    later step) must NOT re-extract — extraction is the billable step
    (Whisper minutes, vision calls)."""
    plat = _platform()
    deps["filer"].move_to_topic_folder.return_value = "f-tech"

    row = _row()
    row.extracted_snapshot = {
        "title": "From snapshot",
        "body_md": "snapshot body",
        "author": "snap-author",
        "published_at": None,
        "media_kind": "text",
        "extra": {},
    }

    await process_capture(
        row, platform=plat, topics=_topics(plat),
        repo=deps["repo"], filer=deps["filer"],
        extract_fn=deps["extract_fn"], classify_fn=deps["classify_fn"],
        templates_repo=deps["templates_repo"], render_fn=deps["render_fn"],
    )

    deps["extract_fn"].assert_not_called()
    # Snapshot is not re-saved when reused.
    deps["repo"].save_extracted_snapshot.assert_not_called()
    # The pipeline still runs to completion off the snapshot content.
    deps["classify_fn"].assert_awaited_once()
    classify_kwargs = deps["classify_fn"].await_args.kwargs
    assert classify_kwargs["extracted"].title == "From snapshot"
    deps["repo"].mark_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_capture_snapshots_immediately_after_extraction(deps):
    """The snapshot must be persisted before classification/render, so a
    failure in any later step never forces re-extraction."""
    plat = _platform()
    deps["filer"].move_to_topic_folder.return_value = "f-tech"
    deps["classify_fn"].side_effect = RuntimeError("anthropic down")

    with pytest.raises(RuntimeError, match="anthropic down"):
        await process_capture(
            _row(), platform=plat, topics=_topics(plat),
            repo=deps["repo"], filer=deps["filer"],
            extract_fn=deps["extract_fn"], classify_fn=deps["classify_fn"],
            templates_repo=deps["templates_repo"], render_fn=deps["render_fn"],
        )

    deps["repo"].save_extracted_snapshot.assert_awaited_once()
    snapshot = deps["repo"].save_extracted_snapshot.await_args.kwargs["snapshot"]
    assert snapshot["title"] == "Hello"


@pytest.mark.asyncio
async def test_process_capture_skips_classify_when_already_classified(deps):
    """Idempotency: row with classifier_topic already set → don't call classify_fn."""
    plat = _platform()
    deps["filer"].move_to_topic_folder.return_value = "f-tech"
    deps["filer"]._mcp.list_folder_tree.return_value = {"totalNodes": 0, "tree": []}

    row = _row()
    # Simulate a row that crashed mid-filing — already classified.
    # The orchestrator pre-fetches via repo.get_by_id to inspect; emulate via attr.
    row.classifier_topic = "Tech"
    row.classifier_conf = 0.92
    row.classifier_reasoning = "from prior attempt"

    await process_capture(
        row,
        platform=plat,
        topics=_topics(plat),
        repo=deps["repo"],
        filer=deps["filer"],
        extract_fn=deps["extract_fn"],
        classify_fn=deps["classify_fn"],
        templates_repo=deps["templates_repo"],
        render_fn=deps["render_fn"],
    )

    deps["classify_fn"].assert_not_called()
    deps["filer"].move_to_topic_folder.assert_awaited_once()


@pytest.mark.asyncio
async def test_orchestrator_uses_resolved_template(deps):
    """Resolve returns a template → orchestrator uses it, no synthesis."""
    plat = _platform()
    deps["filer"].move_to_topic_folder.return_value = "f-tech"

    deps["templates_repo"].resolve = AsyncMock(return_value=_content_template(
        id="t_yt_tut", platform_id="youtube", topic="Tutorials",
    ))
    synth_fn = AsyncMock()  # must NOT be called

    await process_capture(
        _row(), platform=plat, topics=_topics(plat),
        repo=deps["repo"], filer=deps["filer"],
        extract_fn=deps["extract_fn"], classify_fn=deps["classify_fn"],
        templates_repo=deps["templates_repo"], render_fn=deps["render_fn"], synth_fn=synth_fn,
    )

    deps["templates_repo"].resolve.assert_awaited_once()
    synth_fn.assert_not_awaited()
    deps["render_fn"].assert_awaited_once()
    deps["repo"].save_template_run.assert_awaited_once()
    args = deps["repo"].save_template_run.await_args.kwargs
    assert args["template_id"] == "t_yt_tut"


@pytest.mark.asyncio
async def test_orchestrator_synthesizes_template_when_resolve_returns_none(deps):
    """No template anywhere → synthesize then use."""
    plat = _platform()
    deps["filer"].move_to_topic_folder.return_value = "f-tech"

    deps["templates_repo"].resolve = AsyncMock(return_value=None)
    synth_fn = AsyncMock(return_value=_content_template(
        id="t_new", platform_id="article", topic="Tech",
    ))

    await process_capture(
        _row(), platform=plat, topics=_topics(plat),
        repo=deps["repo"], filer=deps["filer"],
        extract_fn=deps["extract_fn"], classify_fn=deps["classify_fn"],
        templates_repo=deps["templates_repo"], render_fn=deps["render_fn"], synth_fn=synth_fn,
    )

    synth_fn.assert_awaited_once()
    kwargs = synth_fn.await_args.kwargs
    assert kwargs["platform_id"] == "article"
    assert kwargs["topic"] == "Tech"


@pytest.mark.asyncio
async def test_orchestrator_persists_template_run(deps):
    """After successful render, save_template_run is called with the
    snapshot of system_prompt and body_md."""
    plat = _platform()
    deps["filer"].move_to_topic_folder.return_value = "f-tech"

    template = _content_template(id="t_x", system_prompt="my prompt v1")
    deps["templates_repo"].resolve = AsyncMock(return_value=template)
    deps["render_fn"].return_value = TemplatedOutput(
        title="T", lede=None, summary_md="- a", body_md="## Body\nContent.",
    )

    await process_capture(
        _row(), platform=plat, topics=_topics(plat),
        repo=deps["repo"], filer=deps["filer"],
        extract_fn=deps["extract_fn"], classify_fn=deps["classify_fn"],
        templates_repo=deps["templates_repo"], render_fn=deps["render_fn"],
    )

    deps["repo"].save_template_run.assert_awaited_once()
    args = deps["repo"].save_template_run.await_args.kwargs
    assert args["template_id"] == "t_x"
    assert args["prompt_used"] == "my prompt v1"
    assert args["output_raw"] == "## Body\nContent."

    deps["repo"].save_extracted_snapshot.assert_awaited_once()
    snapshot_args = deps["repo"].save_extracted_snapshot.await_args.kwargs
    assert snapshot_args["capture_id"] == "01J"
    assert snapshot_args["snapshot"]["title"] == "Hello"


@pytest.mark.asyncio
async def test_orchestrator_renders_lede_as_callout_block(deps):
    """When TemplatedOutput.lede is set, the rendered blocks include
    a `callout` block right after the URL embed and before the Summary."""
    plat = _platform()
    deps["filer"].move_to_topic_folder.return_value = "f-tech"
    deps["render_fn"].return_value = TemplatedOutput(
        title="They Did It",
        lede="TSMC delivered working 1.2nm chips ahead of Intel.",
        summary_md="- a",
        body_md="b",
    )

    await process_capture(
        _row(), platform=plat, topics=_topics(plat),
        repo=deps["repo"], filer=deps["filer"],
        extract_fn=deps["extract_fn"], classify_fn=deps["classify_fn"],
        templates_repo=deps["templates_repo"], render_fn=deps["render_fn"],
    )

    # Inspect append_blocks payload: there should be a callout block
    # containing the lede text, BEFORE the "## Summary" heading.
    append_call = deps["filer"]._mcp.append_blocks.await_args
    blocks = append_call.args[1]
    callout_indices = [i for i, b in enumerate(blocks) if b.get("type") == "callout"]
    summary_heading_indices = [
        i for i, b in enumerate(blocks)
        if b.get("type") == "paragraph" and b.get("style") == "h2"
        and b.get("text") == "Summary"
    ]
    assert len(callout_indices) >= 1
    assert len(summary_heading_indices) >= 1
    assert callout_indices[0] < summary_heading_indices[0]
    # Verify callout text mentions the lede content:
    assert "TSMC" in str(blocks[callout_indices[0]]["text"])


@pytest.mark.asyncio
async def test_orchestrator_renders_failure_callout_when_render_fails(deps):
    """If render_fn raises, the doc body still has a clear failure indicator."""
    plat = _platform()
    deps["filer"].move_to_topic_folder.return_value = "f-tech"
    deps["render_fn"].side_effect = RuntimeError("anthropic broke")

    await process_capture(
        _row(), platform=plat, topics=_topics(plat),
        repo=deps["repo"], filer=deps["filer"],
        extract_fn=deps["extract_fn"], classify_fn=deps["classify_fn"],
        templates_repo=deps["templates_repo"], render_fn=deps["render_fn"],
    )

    # Capture still completes (mark_done called) but with a clear callout.
    deps["repo"].mark_done.assert_awaited_once()
    blocks = deps["filer"]._mcp.append_blocks.await_args.args[1]
    callout_indices = [i for i, b in enumerate(blocks) if b.get("type") == "callout"]
    assert len(callout_indices) == 1
    assert "render failed" in str(blocks[callout_indices[0]]["text"]).lower()
    # save_template_run is NOT called when render failed.
    deps["repo"].save_template_run.assert_not_called()


@pytest.mark.asyncio
async def test_orchestrator_skips_keyframes_appendix_when_template_uses_kf_refs(deps):
    """When the template's `body_md` already embeds `kf:N` references,
    the orchestrator must NOT append a fallback `## Keyframes` heading
    (the template surfaced the keyframes inline already)."""
    plat = _platform()
    deps["filer"].move_to_topic_folder.return_value = "f-tech"

    deps["extract_fn"].return_value = Extracted(
        title="Hello", body_md="Body.", author="a", published_at=None,
        media_kind=MediaKind.VIDEO,
        extra={"keyframes": [
            {"timestamp_seconds": 1.0, "caption": "frame zero", "blob_source_id": "blob0"},
            {"timestamp_seconds": 2.0, "caption": "frame one", "blob_source_id": "blob1"},
        ]},
    )
    # Template's body_md DOES use kf:0 — so the fallback should be skipped.
    deps["render_fn"].return_value = TemplatedOutput(
        title="T", lede=None, summary_md="- a",
        body_md="## Section\n\n![the frame](kf:0)\n\nMore text.",
    )

    await process_capture(
        _row(), platform=plat, topics=_topics(plat),
        repo=deps["repo"], filer=deps["filer"],
        extract_fn=deps["extract_fn"], classify_fn=deps["classify_fn"],
        templates_repo=deps["templates_repo"], render_fn=deps["render_fn"],
    )

    blocks = deps["filer"]._mcp.append_blocks.await_args.args[1]
    keyframes_headings = [
        b for b in blocks
        if b.get("type") == "paragraph" and b.get("style") == "h2"
        and b.get("text") == "Keyframes"
    ]
    assert len(keyframes_headings) == 0, \
        "Fallback ## Keyframes section must not appear when template uses kf:N"


@pytest.mark.asyncio
async def test_orchestrator_emits_keyframes_appendix_when_template_does_not_use_kf_refs(deps):
    """Fallback path: when keyframes exist but template's body_md doesn't
    reference any, orchestrator appends a `## Keyframes` h2 + image blocks
    backed by the keyframes' blob_source_ids."""
    plat = _platform()
    deps["filer"].move_to_topic_folder.return_value = "f-tech"

    deps["extract_fn"].return_value = Extracted(
        title="Hello", body_md="Body.", author="a", published_at=None,
        media_kind=MediaKind.VIDEO,
        extra={"keyframes": [
            {"timestamp_seconds": 1.0, "caption": "IDE", "blob_source_id": "blob0"},
            {"timestamp_seconds": 2.5, "caption": "Network panel", "blob_source_id": "blob1"},
            {"timestamp_seconds": 4.0, "caption": "Final result", "blob_source_id": "blob2"},
            {"timestamp_seconds": 6.0, "caption": "Outro", "blob_source_id": "blob3"},
        ]},
    )
    # Template's body_md does NOT reference any kf:N — fallback kicks in.
    deps["render_fn"].return_value = TemplatedOutput(
        title="T", lede=None, summary_md="- a",
        body_md="## Section\n\nNo keyframe references at all.",
    )

    await process_capture(
        _row(), platform=plat, topics=_topics(plat),
        repo=deps["repo"], filer=deps["filer"],
        extract_fn=deps["extract_fn"], classify_fn=deps["classify_fn"],
        templates_repo=deps["templates_repo"], render_fn=deps["render_fn"],
    )

    blocks = deps["filer"]._mcp.append_blocks.await_args.args[1]
    # Exactly one ## Keyframes h2 heading appears.
    keyframes_heading_indices = [
        i for i, b in enumerate(blocks)
        if b.get("type") == "paragraph" and b.get("style") == "h2"
        and b.get("text") == "Keyframes"
    ]
    assert len(keyframes_heading_indices) == 1
    # The blocks AFTER the heading include affine:image blocks for the keyframes.
    after_heading = blocks[keyframes_heading_indices[0] + 1:]
    image_blocks = [b for b in after_heading if b.get("type") == "image"]
    # All available keyframes (up to cap) get an image block in the fallback.
    assert len(image_blocks) == 4
    source_ids = [b.get("sourceId") for b in image_blocks]
    assert source_ids == ["blob0", "blob1", "blob2", "blob3"]
    # Captions land on the image blocks.
    captions = [b.get("caption") for b in image_blocks]
    assert "IDE" in captions
    assert "Network panel" in captions
    # The fallback appears AFTER the body_md content but BEFORE the Source: footer.
    source_indices = [
        i for i, b in enumerate(blocks)
        if b.get("type") == "paragraph" and b.get("style") == "text"
        and isinstance(b.get("text"), list)
        and any(
            isinstance(op, dict) and op.get("text") == "Source: "
            for op in b["text"]
        )
    ]
    assert source_indices and keyframes_heading_indices[0] < source_indices[0]


@pytest.mark.asyncio
async def test_orchestrator_does_not_emit_keyframes_appendix_when_no_keyframes(deps):
    """When the source has no keyframes at all (e.g. text-only article,
    or video where vision analysis returned no frames), no fallback
    section should appear."""
    plat = _platform()
    deps["filer"].move_to_topic_folder.return_value = "f-tech"
    # No keyframes in extra.
    deps["extract_fn"].return_value = Extracted(
        title="x", body_md="Body.", author=None, published_at=None,
        media_kind=MediaKind.TEXT, extra={},
    )
    deps["render_fn"].return_value = TemplatedOutput(
        title="T", lede=None, summary_md="- a", body_md="b",
    )

    await process_capture(
        _row(), platform=plat, topics=_topics(plat),
        repo=deps["repo"], filer=deps["filer"],
        extract_fn=deps["extract_fn"], classify_fn=deps["classify_fn"],
        templates_repo=deps["templates_repo"], render_fn=deps["render_fn"],
    )

    blocks = deps["filer"]._mcp.append_blocks.await_args.args[1]
    keyframes_headings = [
        b for b in blocks
        if b.get("type") == "paragraph" and b.get("style") == "h2"
        and b.get("text") == "Keyframes"
    ]
    assert len(keyframes_headings) == 0


@pytest.mark.asyncio
async def test_orchestrator_appends_raw_transcript_as_separate_section(deps):
    """Raw extracted.body_md must always render under `## Transcript` so the
    source signal is preserved even when the template's body_md is a
    summary. Production bug: Phase 14 v1 dropped the transcript entirely."""
    plat = _platform()
    deps["filer"].move_to_topic_folder.return_value = "f-tech"
    deps["extract_fn"].return_value = Extracted(
        title="Hello",
        body_md="**Travis Scott:** I built this in one day...\n\nMore words.",
        author="a", published_at=None, media_kind=MediaKind.VIDEO, extra={},
    )
    deps["render_fn"].return_value = TemplatedOutput(
        title="T", lede=None, summary_md="- a", body_md="## Analysis\nStructured view.",
    )

    await process_capture(
        _row(), platform=plat, topics=_topics(plat),
        repo=deps["repo"], filer=deps["filer"],
        extract_fn=deps["extract_fn"], classify_fn=deps["classify_fn"],
        templates_repo=deps["templates_repo"], render_fn=deps["render_fn"],
    )

    blocks = deps["filer"]._mcp.append_blocks.await_args.args[1]
    transcript_heading_indices = [
        i for i, b in enumerate(blocks)
        if b.get("type") == "paragraph" and b.get("style") == "h2"
        and b.get("text") == "Transcript"
    ]
    assert len(transcript_heading_indices) == 1, \
        "exactly one ## Transcript heading expected"

    # Transcript section comes AFTER the body_md analysis, BEFORE the Source: footer.
    transcript_idx = transcript_heading_indices[0]
    source_indices = [
        i for i, b in enumerate(blocks)
        if b.get("type") == "paragraph" and b.get("style") == "text"
        and isinstance(b.get("text"), list)
        and any(op.get("text") == "Source: " for op in b["text"] if isinstance(op, dict))
    ]
    assert source_indices and transcript_idx < source_indices[0]

    # The transcript content (rendered blocks) appears immediately after the heading.
    # At minimum, the source text "Travis Scott" survives into one of the trailing blocks.
    after_transcript = blocks[transcript_idx + 1:]
    flat = " ".join(
        str(b.get("text", "")) for b in after_transcript
    )
    assert "Travis Scott" in flat


@pytest.mark.asyncio
async def test_orchestrator_does_not_emit_callout_for_whitespace_only_lede(deps):
    """Empty-ish ledes (Sonnet sometimes returns `' '` or `'\\n'`) must not
    produce a callout block. Production was showing icon-only empty callouts
    between the URL embed and Summary heading from this exact case."""
    plat = _platform()
    deps["filer"].move_to_topic_folder.return_value = "f-tech"

    for lede_value in (None, "", "   ", "\n  \n"):
        deps["filer"]._mcp.append_blocks.reset_mock()
        deps["render_fn"].return_value = TemplatedOutput(
            title="T", lede=lede_value,
            summary_md="- a", body_md="b",
        )

        await process_capture(
            _row(), platform=plat, topics=_topics(plat),
            repo=deps["repo"], filer=deps["filer"],
            extract_fn=deps["extract_fn"], classify_fn=deps["classify_fn"],
            templates_repo=deps["templates_repo"], render_fn=deps["render_fn"],
        )

        blocks = deps["filer"]._mcp.append_blocks.await_args.args[1]
        callouts = [b for b in blocks if b.get("type") == "callout"]
        assert len(callouts) == 0, \
            f"no callout expected for lede={lede_value!r}, got: {callouts}"


@pytest.mark.asyncio
async def test_orchestrator_routes_long_body_to_chunked_render(deps, monkeypatch):
    """Body length exceeding settings.chunked_render_threshold_chars must
    route through chunked_render, not the single-call render_fn."""
    from src.config import settings as cfg
    monkeypatch.setattr(cfg, "chunked_render_threshold_chars", 100)

    plat = _platform()
    deps["filer"].move_to_topic_folder.return_value = "f-tech"
    long_body = "x" * 500
    deps["extract_fn"].return_value = Extracted(
        title="Long", body_md=long_body, author="a", published_at=None,
        media_kind=MediaKind.VIDEO, extra={},
    )

    chunked_mock = AsyncMock(return_value=TemplatedOutput(
        title="From chunked", lede=None, summary_md="- a", body_md="b",
    ))
    monkeypatch.setattr("src.pipeline.orchestrator.chunked_render", chunked_mock)

    await process_capture(
        _row(), platform=plat, topics=_topics(plat),
        repo=deps["repo"], filer=deps["filer"],
        extract_fn=deps["extract_fn"], classify_fn=deps["classify_fn"],
        templates_repo=deps["templates_repo"], render_fn=deps["render_fn"],
    )

    chunked_mock.assert_awaited_once()
    deps["render_fn"].assert_not_awaited()
    # Title surfaced from chunked path.
    deps["filer"]._mcp.set_doc_title.assert_awaited_with("d-1", "From chunked")


@pytest.mark.asyncio
async def test_orchestrator_routes_short_body_to_single_call(deps, monkeypatch):
    """Body length under threshold uses the existing single-call render_fn."""
    from src.config import settings as cfg
    monkeypatch.setattr(cfg, "chunked_render_threshold_chars", 100_000)

    plat = _platform()
    deps["filer"].move_to_topic_folder.return_value = "f-tech"
    # The default _extracted() body is ~25 chars, well under 100k threshold.

    chunked_mock = AsyncMock()
    monkeypatch.setattr("src.pipeline.orchestrator.chunked_render", chunked_mock)

    await process_capture(
        _row(), platform=plat, topics=_topics(plat),
        repo=deps["repo"], filer=deps["filer"],
        extract_fn=deps["extract_fn"], classify_fn=deps["classify_fn"],
        templates_repo=deps["templates_repo"], render_fn=deps["render_fn"],
    )

    chunked_mock.assert_not_awaited()
    deps["render_fn"].assert_awaited_once()


@pytest.mark.asyncio
async def test_orchestrator_omits_transcript_when_extracted_body_is_empty(deps):
    """If the source has no body (e.g. shared_text-only or stub capture),
    the renderer must not emit an empty `## Transcript` section."""
    plat = _platform()
    deps["filer"].move_to_topic_folder.return_value = "f-tech"
    deps["extract_fn"].return_value = Extracted(
        title="x", body_md="", author=None, published_at=None,
        media_kind=MediaKind.TEXT, extra={},
    )
    deps["render_fn"].return_value = TemplatedOutput(
        title="T", lede=None, summary_md="- a", body_md="b",
    )

    await process_capture(
        _row(), platform=plat, topics=_topics(plat),
        repo=deps["repo"], filer=deps["filer"],
        extract_fn=deps["extract_fn"], classify_fn=deps["classify_fn"],
        templates_repo=deps["templates_repo"], render_fn=deps["render_fn"],
    )

    blocks = deps["filer"]._mcp.append_blocks.await_args.args[1]
    transcript_headings = [
        b for b in blocks
        if b.get("type") == "paragraph" and b.get("style") == "h2"
        and b.get("text") == "Transcript"
    ]
    assert len(transcript_headings) == 0
