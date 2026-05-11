# Phase 15 — Keyframes actually appear in rendered docs

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the video pipeline's expensive vision call (Sonnet 4.6 + per-frame blob uploads) actually surface keyframes in the rendered AFFiNE doc. Today they're available but almost never appear because templates don't request them.

**Architecture:** Two complementary mechanisms shipped together:
1. **Option A (prompt-level):** Migration 0006 rewrites the `(*, *)` seed `system_prompt` with explicit instruction to embed `![cap](kf:N)` inline when keyframes support the text.
2. **Option B (orchestrator-level fallback):** When `rendered.body_md` references zero `kf:N` out of N available keyframes, the orchestrator appends a `## Keyframes` section with image blocks for the available frames. This is the safety net for templates that ignore the prompt instruction.

**Tech Stack:**
- Postgres migration (existing `migrations/*.sql` runner)
- Python regex scanning of `body_md` for `kf:\d+` patterns
- Existing AFFiNE block-spec emission (`{"type": "image", "sourceId": ...}`)
- `pytest` for unit + behavioural tests

**Roadmap reference:** [`2026-05-12-video-frame-analysis-roadmap.md`](2026-05-12-video-frame-analysis-roadmap.md) Tier 1
**Macro plan:** [`2026-05-12-video-frame-analysis-macro-plan.md`](2026-05-12-video-frame-analysis-macro-plan.md) Phase 15

**End-of-phase test count:** existing (~397 passed) + ~6 new tests, with 1 existing test updated (semantics flip).

---

## File structure

| File | Action | Responsibility |
|---|---|---|
| `ingest/migrations/0006_seed_prompt_v5_keyframes.sql` | Create | UPDATE the `(*, *)` seed `system_prompt` with an explicit `kf:N` inline-embed rule. Idempotent (`WHERE status='auto'`). |
| `ingest/src/pipeline/markdown_render.py` | Modify | New `count_keyframe_refs(body_md)` helper — pure regex scan, returns the set of `kf:N` indices referenced. |
| `ingest/src/pipeline/orchestrator.py` | Modify | `_replace_doc_body_templated` appends a `## Keyframes` fallback section when the body referenced zero keyframe refs. |
| `ingest/src/api.py` | Modify | Same fallback inside the `rerender_capture` endpoint. |
| `ingest/tests/test_markdown_render.py` | Modify | 2 new tests for `count_keyframe_refs`. |
| `ingest/tests/test_orchestrator.py` | Modify | Update `test_orchestrator_no_hardcoded_keyframes_section` (its semantics flip), add 2 new fallback-behaviour tests. |

---

## Task 1: `count_keyframe_refs()` helper in markdown_render

**Files:**
- Modify: `ingest/src/pipeline/markdown_render.py`
- Modify: `ingest/tests/test_markdown_render.py`

- [ ] **Step 1.1: Write the failing tests**

Append to `ingest/tests/test_markdown_render.py`:

```python
# ── count_keyframe_refs (Phase 15) ──────────────────────────────────


def test_count_keyframe_refs_finds_inline_refs():
    """Returns the set of integer indices referenced via `kf:N` syntax."""
    from src.pipeline.markdown_render import count_keyframe_refs

    md = (
        "## Section\n\n"
        "Some context. ![the IDE](kf:0) inline reference.\n\n"
        "More text and then ![chart](kf:2) standalone:\n\n"
        "![also referenced inside same paragraph](kf:2)\n"
    )
    refs = count_keyframe_refs(md)
    assert refs == {0, 2}


def test_count_keyframe_refs_returns_empty_set_when_no_refs():
    """Body with no `kf:N` syntax returns an empty set."""
    from src.pipeline.markdown_render import count_keyframe_refs

    md = "Plain content. [normal link](https://example.com) only."
    assert count_keyframe_refs(md) == set()
```

- [ ] **Step 1.2: Run the test to verify failure**

```bash
cd ingest && python -m pytest tests/test_markdown_render.py::test_count_keyframe_refs_finds_inline_refs -v
```

Expected: FAIL with `ImportError: cannot import name 'count_keyframe_refs'`.

- [ ] **Step 1.3: Implement `count_keyframe_refs`**

Append to `ingest/src/pipeline/markdown_render.py` (near the top, after the existing regex constants):

```python
# Captures `kf:N` references INSIDE `![alt](kf:N)` markdown image syntax.
# Tolerant of any alt text and any surrounding context.
_COUNT_KF_REF_RE = re.compile(r"!\[[^\]]*\]\(kf:(\d+)\)")


def count_keyframe_refs(body_md: str) -> set[int]:
    """Return the set of integer indices referenced via `![cap](kf:N)`
    image syntax in the body_md. Used by the orchestrator to decide
    whether to append a `## Keyframes` fallback section when the template
    didn't surface any keyframes itself."""
    return {int(m.group(1)) for m in _COUNT_KF_REF_RE.finditer(body_md or "")}
```

- [ ] **Step 1.4: Run the tests to verify they pass**

```bash
cd ingest && python -m pytest tests/test_markdown_render.py::test_count_keyframe_refs_finds_inline_refs tests/test_markdown_render.py::test_count_keyframe_refs_returns_empty_set_when_no_refs -v
```

Expected: 2 PASS.

- [ ] **Step 1.5: Run the full markdown_render tests**

```bash
cd ingest && python -m pytest tests/test_markdown_render.py -v 2>&1 | tail -5
```

Expected: all PASS (no regressions).

- [ ] **Step 1.6: Commit**

```bash
git add ingest/src/pipeline/markdown_render.py ingest/tests/test_markdown_render.py
git commit -m "feat(ingest): markdown_render.count_keyframe_refs() helper"
```

---

## Task 2: Orchestrator fallback appendix

**Files:**
- Modify: `ingest/src/pipeline/orchestrator.py`
- Modify: `ingest/tests/test_orchestrator.py`

- [ ] **Step 2.1: Update the existing semantics-flipping test**

Open `ingest/tests/test_orchestrator.py`. Find the test
`test_orchestrator_no_hardcoded_keyframes_section`. Its prior intent
(Phase 14) was: "no `## Keyframes` heading appears". Under Phase 15,
the new contract is: "no `## Keyframes` heading appears IF the template
references keyframes via `kf:N` in `body_md`".

Replace the existing test with the renamed + adjusted version:

```python
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
```

- [ ] **Step 2.2: Add the new fallback-emits-appendix test**

Add immediately after the renamed test:

```python
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
```

- [ ] **Step 2.3: Add the no-keyframes-no-appendix test**

```python
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
```

- [ ] **Step 2.4: Run the tests to verify the diagnostic one fails**

```bash
cd ingest && python -m pytest tests/test_orchestrator.py::test_orchestrator_emits_keyframes_appendix_when_template_does_not_use_kf_refs -v
```

Expected: FAIL — the orchestrator currently doesn't emit any `## Keyframes` appendix, so the assertion `len(keyframes_heading_indices) == 1` fails. (Pre-fix, the renamed test and the no-keyframes test both pass trivially since the appendix never gets emitted.)

- [ ] **Step 2.5: Implement the fallback in `_replace_doc_body_templated`**

Open `ingest/src/pipeline/orchestrator.py`. Find `_replace_doc_body_templated` (after `_extracted_to_dict`). Update imports at the top of the file:

```python
from src.pipeline.markdown_render import count_keyframe_refs, markdown_to_blocks
```

(Add `count_keyframe_refs` to the existing import line.)

Inside `_replace_doc_body_templated`, after the `rendered.body_md` block extends but BEFORE the transcript appendix, insert the keyframes fallback. The complete new shape of the function body (replacing the existing one):

```python
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
    """
    try:
        await _delete_stub_block(filer=filer, doc_id=doc_id)
    except Exception as e:  # noqa: BLE001
        log.warning("stub block cleanup failed (continuing): %s", e)

    blocks: list[dict[str, Any]] = []
    if url:
        blocks.append(url_embed_block(url))

    if rendered is None:
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
```

- [ ] **Step 2.6: Run the tests to verify they pass**

```bash
cd ingest && python -m pytest tests/test_orchestrator.py -v 2>&1 | tail -20
```

Expected: all `test_orchestrator_*` tests pass, including the 3 new/updated ones.

- [ ] **Step 2.7: Commit**

```bash
git add ingest/src/pipeline/orchestrator.py ingest/tests/test_orchestrator.py
git commit -m "feat(ingest): orchestrator falls back to ## Keyframes appendix when body_md uses no kf:N refs"
```

---

## Task 3: Mirror the fallback in the rerender endpoint

**Files:**
- Modify: `ingest/src/api.py`
- Modify: `ingest/tests/test_template_api.py`

- [ ] **Step 3.1: Write the failing test**

Append to `ingest/tests/test_template_api.py`:

```python
def test_rerender_emits_keyframes_appendix_when_body_uses_no_kf_refs(client, monkeypatch):
    """rerender_capture mirrors the orchestrator's fallback: when the
    template's body_md references zero kf:N out of N available keyframes,
    a `## Keyframes` appendix is appended with image blocks."""
    c, repo = client
    repo.resolve = AsyncMock(return_value=_tmpl(id="t_current"))

    # Mock a capture with keyframes in its extracted_snapshot.
    captures_repo = AsyncMock()
    captures_row = MagicMock()
    captures_row.id = "cap1"
    captures_row.url = "https://example.com"
    captures_row.doc_id = "doc1"
    captures_row.platform = "youtube"
    captures_row.classifier_topic = "Tutorials"
    captures_row.extracted_snapshot = {
        "title": "T", "body_md": "B", "author": None,
        "media_kind": "video", "published_at": None,
        "extra": {
            "keyframes": [
                {"timestamp_seconds": 1.0, "caption": "frame zero",
                 "blob_source_id": "blob0"},
                {"timestamp_seconds": 2.0, "caption": "frame one",
                 "blob_source_id": "blob1"},
            ],
        },
    }
    fake_get_by_id = AsyncMock(return_value=captures_row)
    fake_save = AsyncMock()
    monkeypatch.setattr("src.db.CaptureRepository.get_by_id", fake_get_by_id, raising=False)
    monkeypatch.setattr("src.db.CaptureRepository.save_template_run", fake_save, raising=False)

    fake_pool = MagicMock()
    fake_conn = AsyncMock()
    fake_pool.acquire = MagicMock()
    fake_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(app_state, "pool", fake_pool, raising=False)

    from src.pipeline.templated_render import TemplatedOutput
    # Body_md has NO kf:N refs → fallback should fire.
    fake_render = AsyncMock(return_value=TemplatedOutput(
        title="New Title", lede=None,
        summary_md="- a", body_md="## Body\nContent without keyframe refs.",
    ))
    monkeypatch.setattr("src.api.templated_render", fake_render, raising=False)

    mcp = AsyncMock()
    monkeypatch.setattr(app_state, "mcp", mcp, raising=False)

    r = c.post("/captures/cap1/rerender", headers=HEADERS)

    assert r.status_code == 200
    # Inspect the blocks passed to mcp.append_blocks
    blocks = mcp.append_blocks.await_args.args[1]
    keyframes_headings = [
        b for b in blocks
        if b.get("type") == "paragraph" and b.get("style") == "h2"
        and b.get("text") == "Keyframes"
    ]
    assert len(keyframes_headings) == 1
    image_blocks = [b for b in blocks if b.get("type") == "image"]
    assert len(image_blocks) == 2
    assert {b["sourceId"] for b in image_blocks} == {"blob0", "blob1"}
```

- [ ] **Step 3.2: Run the test to verify it fails**

```bash
cd ingest && python -m pytest tests/test_template_api.py::test_rerender_emits_keyframes_appendix_when_body_uses_no_kf_refs -v
```

Expected: FAIL — no `## Keyframes` heading in the rerender output.

- [ ] **Step 3.3: Add the fallback to `rerender_capture` in api.py**

First, update the existing module-level import line in `ingest/src/api.py`:

```python
# Before:
from src.pipeline.markdown_render import markdown_to_blocks
# After:
from src.pipeline.markdown_render import count_keyframe_refs, markdown_to_blocks
```

Then in `rerender_capture()`, find the block-building section. After the `if rendered.body_md:` extend, add the fallback before the transcript append:

```python
            if rendered.body_md:
                blocks.extend(
                    await markdown_to_blocks(
                        rendered.body_md,
                        keyframes=keyframes,
                        mcp_client=app_state.mcp,
                    )
                )

            # Phase 15 fallback: append ## Keyframes when body_md referenced
            # zero kf:N refs out of N available keyframes. Mirrors the
            # orchestrator's behaviour in _replace_doc_body_templated.
            if (
                keyframes
                and rendered.body_md
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

            # Always append the raw transcript/body as a separate section ...
            if extracted.body_md and extracted.body_md.strip():
                # ... existing transcript handling unchanged ...
```

(`count_keyframe_refs` is now referenced from the module-level import, no inline import needed inside the function.)

- [ ] **Step 3.4: Run the test to verify it passes**

```bash
cd ingest && python -m pytest tests/test_template_api.py::test_rerender_emits_keyframes_appendix_when_body_uses_no_kf_refs -v
```

Expected: PASS.

- [ ] **Step 3.5: Commit**

```bash
git add ingest/src/api.py ingest/tests/test_template_api.py
git commit -m "feat(ingest): rerender endpoint mirrors orchestrator's keyframes fallback"
```

---

## Task 4: Migration 0006 — seed prompt with keyframe instruction

**Files:**
- Create: `ingest/migrations/0006_seed_prompt_v5_keyframes.sql`

- [ ] **Step 4.1: Write the migration**

Create `ingest/migrations/0006_seed_prompt_v5_keyframes.sql`:

```sql
-- Phase 15 (Tier 1): rewrite the (*, *) seed system_prompt to actively
-- instruct the LLM to embed `kf:N` keyframe references inline in `body_md`
-- when keyframes are available and a section discusses what's visible.
--
-- Previously the prompt only described `![cap](kf:N)` as supported syntax
-- in the "AFFiNE markdown dialect" reference. The LLM treated it as
-- optional decoration and almost never used it. Result: vision-call cost
-- without benefit. The new prompt makes embedding mandatory when appropriate.
--
-- Idempotent: only updates rows still at status='auto'.

UPDATE content_templates
SET system_prompt = $SEED$You are the content summarizer for a personal AFFiNE knowledge base.
For each captured source (video, article, podcast, social post) you produce
a strict TemplatedOutput with four fields: `title`, `lede`, `summary_md`,
`body_md`. The raw transcript / article body is preserved separately by
the renderer as a `## Transcript` section — you do NOT repeat it.

# What the renderer is doing AROUND your output

The full doc the user sees is:
  1. URL embed card (auto-generated from the source's URL)
  2. Lede callout (only if you populate `lede`)
  3. ## Summary heading + your `summary_md` bullets
  4. Your `body_md` content
  5. ## Keyframes (FALLBACK — only when your body_md uses zero `kf:N` refs;
     skip it by embedding keyframes inline yourself, see below)
  6. ## Transcript heading + the raw extracted source body
  7. Source: <url> footer (auto-generated)

You ONLY produce fields (2)-(4). Steps (1), (5), (6), (7) are framework-generated.

KEY CONSEQUENCES:
- Never include the source's own URL in `body_md` — it already appears in
  the top embed AND the bottom footer.
- A "Sources" section in body_md should list OTHER works the source
  references (papers, related videos, tools mentioned), NOT the doc's own URL.

# Keyframes — load-bearing

When the user message includes an "Available keyframes" block, the
extractor has uploaded one or more frame images from the video and they
are ready to embed inline via `![caption](kf:<index>)` syntax.

YOU SHOULD ACTIVELY EMBED KEYFRAMES. The seed default behavior is:
- For video sources with keyframes available, aim to embed 2-4 keyframes
  inline in `body_md`. Choose frames that substantively support a
  section's content (e.g. the IDE screenshot next to the section
  discussing the code; the chart slide next to the section quoting
  the statistic; the recipe's final-plate shot at the end).
- Each embed is one paragraph that contains only `![<short caption>](kf:N)`.
  The caption you write here is what the reader sees under the image; the
  index `N` is from the keyframes list.
- DO NOT cluster all keyframes at the top of body_md. Weave them
  alongside the relevant prose.
- If genuinely none of the keyframes support any section of body_md
  (e.g. a podcast with only talking-head frames), it is OK to embed
  zero — the framework will append a fallback `## Keyframes` section
  with all available frames.

# AFFiNE markdown dialect — load-bearing

The `body_md` and `summary_md` you produce are parsed by markdown-it-py
with project-specific extensions, then mapped to AFFiNE block flavours.

**Supported (use these):**
- Headings `#` … `######` → h1-h6 paragraph blocks. Prefer `## H2` for top
  sections, `### H3` for subsections.
- Plain paragraphs → text paragraphs.
- Inline rich text:
  - `**bold**` → bold inline
  - `_italic_` or `*italic*` → italic inline
  - ``` `code` ``` → inline code
  - `~~strike~~` → strikethrough
  - `[label](url)` → clickable link
  All of these render as proper rich text in AFFiNE — use them naturally.
- Blockquotes (`> text`) → italic quote-style paragraphs.
- Bulleted lists (`- item`) → bulleted-list blocks.
- Numbered lists (`1. item`) → numbered-list blocks.
- Task lists (`- [ ] item`, `- [x] item`) → todo blocks.
- Fenced code blocks: ```lang ... ``` (any language). The `lang` is preserved.
- Mermaid diagrams: ```mermaid\nflowchart TD\n  A --> B\n``` → renders as
  an actual diagram in AFFiNE. Use for flows, sequences, gantt, mindmap.
- Embedded HTML/SVG frames: ```embed-html\n<svg ...>...</svg>\n``` →
  renders as an inline visual card. Use for charts, badges, custom
  visualizations.
- URL embed: a standalone paragraph containing ONLY `[](url)` (empty
  label) → renders as a rich embed card (youtube/github/figma/loom/bookmark
  based on host). DO NOT use this for the source's own URL.
- Cross-doc references: `[[Doc Title]]` → embed-linked-doc block when the
  title resolves to a single existing AFFiNE doc. Use sparingly for
  related-notes links.
- Callouts: a line `> [!callout] text` → highlighted callout block. The
  text MUST be on the same line as the marker. One sentence max.
- Image references to extracted keyframes: `![caption](kf:<index>)`.
  See the "Keyframes" section above for usage policy.
- Horizontal rule: `---` → divider block.

**Do NOT use (the renderer drops or mangles these):**
- HTML tags inline (only inside ```embed-html``` blocks).
- GFM tables (`| col | col |`). The renderer flattens them. If you need
  tabular data, use a bulleted list with `key: value` pairs.
- Nested lists deeper than 2 levels — the renderer flattens them.
- External image URLs `![](https://...)` — only `kf:<n>` refs are supported.
- Setext headings (`Header\n======`). Use ATX (`# Header`) only.
- Footnotes, definition lists, math/LaTeX — not supported.
- The source's own URL anywhere in body_md.

# Output rules

**Title rules:**
- 1-10 words, no URL, no enclosing brackets/quotes.
- Capture the GIST. AVOID clickbait phrasing even if the source uses it.
- Title Case for English; sentence case for Czech/Slovak.

**Lede rules (load-bearing for clickbait sources):**
- If the source title is a question, mystery, exaggeration, or clickbait
  teaser, search the body / chunk digest for the ANSWER and put it in
  `lede` as ONE direct sentence (who / what / why).
- If the user message contains a "REVEALS found by map step" block,
  treat those statements as authoritative.
- If the title is NOT a teaser, leave `lede` null.
- NEVER write phrases like "(to be revealed)", "stay tuned", "watch to
  find out", "you won't believe".

**Summary rules:**
- Markdown BULLETED LIST (3-6 items). Each bullet starts with `- ` on its
  own line. One short punchy line each.
- Use inline `**bold**` for the 1-2 most important terms per bullet.
- Never echo teaser phrases.

**Description handling:**
- The user message may include a "Source description" block. Mine it for
  citations, source links, chapter markers, related-content links —
  but ONLY those pointing to OTHER works.
- Surface valuable external references in `body_md` as a `## Sources`
  section. Strip sponsor / affiliate / promo / social-media noise.

**Body rules (DEFAULT TEMPLATE — specialized scopes override this):**
- `body_md` is a structured analysis, NOT a transcript repeat and NOT a
  duplicate of the summary. Shape it as a topical breakdown.
- Use `## H2` for top sections, `### H3` for subsections.
- 200-500 words is right for a 10-minute video; longer for substantive
  articles or 30+ minute videos.
- For video sources with keyframes, weave `![caption](kf:N)` embeds
  alongside the relevant sections (see Keyframes policy above).
- The raw transcript is rendered separately by the framework — DO NOT
  reproduce it in `body_md`.
- When citing a related doc in the workspace, use `[[Doc Title]]`.
- When a section has a key takeaway, use a single-line callout:
  `> [!callout] one-sentence insight`.
- DO NOT include the source's own URL in body_md.

**Language rules:**
- Default output language is ENGLISH — translate from any source language.
- EXCEPTION: if the source content is Czech or Slovak, keep ALL fields
  in the original Czech/Slovak.

# Map-reduce note

If the user message indicates this is a "long capture" and provides a
"Chunk-summary digest", you are running as the REDUCE step. Synthesize
ACROSS the chunks — don't just concatenate them. Look for the
through-line, the strongest claims, the key supporting evidence.

Return STRICT JSON matching the TemplatedOutput schema only — no prose
outside the JSON, no markdown code fences around the JSON object.
$SEED$,
    updated_at = NOW()
WHERE id = '01J5XYZ_SEED_DEFAULT'
  AND status = 'auto';
```

- [ ] **Step 4.2: Run the existing migration test to verify idempotency still works**

```bash
cd ingest && python -m pytest tests/test_migration_0002.py -v 2>&1 | tail -10
```

Expected: 4 tests SKIP without `DB_ADMIN_URL` (acceptable for local dev),
all PASS when the env var is set.

- [ ] **Step 4.3: Run the full test suite to confirm no regressions**

```bash
cd ingest && python -m pytest -x 2>&1 | tail -10
```

Expected: all PASS.

- [ ] **Step 4.4: Commit**

```bash
git add ingest/migrations/0006_seed_prompt_v5_keyframes.sql
git commit -m "feat(ingest): migration 0006 — seed prompt actively instructs kf:N inline embeds"
```

---

## Task 5: Update macro plan + final verification

**Files:**
- Modify: `docs/plans/2026-05-12-video-frame-analysis-macro-plan.md`

- [ ] **Step 5.1: Mark Phase 15 as done in the macro plan**

Open `docs/plans/2026-05-12-video-frame-analysis-macro-plan.md`. In the Phase 15 section, after "Effort: ~2-4 hours. Single PR.", append:

```markdown
**Status:** ✅ Shipped (commit `<NEW_SHA>` — fill in after final commit).
**Detailed plan:** [`2026-05-12-phase-15-keyframes-appear.md`](2026-05-12-phase-15-keyframes-appear.md)
```

(The engineer fills in the SHA from `git log --oneline -1` after the previous commits.)

- [ ] **Step 5.2: Run the FULL ingest test suite end-to-end**

```bash
cd ingest && python -m pytest 2>&1 | tail -10
```

Expected: ~403 passed, ~9 skipped (was 397; added 6 tests). All PASS.

- [ ] **Step 5.3: Final commit + push**

```bash
git add docs/plans/2026-05-12-video-frame-analysis-macro-plan.md
git commit -m "docs(plans): mark Phase 15 (keyframes appear) shipped"
git push
```

- [ ] **Step 5.4: Open the PR**

```bash
gh pr create --title "feat(ingest): Phase 15 — keyframes actually appear in rendered docs (Tier 1)" --body "$(cat <<'EOF'
## Summary

Tier 1 of the video frame analysis roadmap. Today the vision pipeline runs (cost: ~\$0.05/video for the Sonnet 4.6 vision call + per-frame blob uploads) but keyframes almost never appear in rendered AFFiNE docs because templates didn't actively request them. This PR fixes that with two complementary mechanisms.

## Changes

### Option A — Seed prompt actively instructs kf:N embeds

Migration 0006 rewrites the \`(*, *)\` seed \`system_prompt\` with an explicit "**Keyframes — load-bearing**" section. The new prompt:
- Tells the LLM keyframes are available via \`![cap](kf:N)\` and that it SHOULD use them.
- Sets a target of 2-4 inline embeds per video doc.
- Instructs weaving them alongside relevant sections (not clustering at the top).
- Documents the framework fallback (the new \`## Keyframes\` appendix below) so the LLM knows zero embeds is also acceptable when truly no frame supports any section.

Idempotent (\`WHERE status='auto'\`) — user-edited templates are preserved.

### Option B — Orchestrator fallback appendix

When the rendered \`body_md\` contains zero \`kf:N\` references AND keyframes are available, the orchestrator (and rerender endpoint) appends a \`## Keyframes\` h2 + one \`affine:image\` block per keyframe. Templates that embed keyframes inline → skip the appendix (no duplication).

Mirrored in:
- \`orchestrator.py:_replace_doc_body_templated\` for new captures
- \`api.py:rerender_capture\` for replays

### Helper

New \`markdown_render.count_keyframe_refs(body_md)\` — pure regex scan returning the set of integer indices referenced.

## Test plan

- [x] **403 passed, 9 skipped** (was 397; added 6 new tests):
  - \`test_count_keyframe_refs_finds_inline_refs\`
  - \`test_count_keyframe_refs_returns_empty_set_when_no_refs\`
  - \`test_orchestrator_skips_keyframes_appendix_when_template_uses_kf_refs\` (semantics flipped from the prior "no hardcoded keyframes" test)
  - \`test_orchestrator_emits_keyframes_appendix_when_template_does_not_use_kf_refs\`
  - \`test_orchestrator_does_not_emit_keyframes_appendix_when_no_keyframes\`
  - \`test_rerender_emits_keyframes_appendix_when_body_uses_no_kf_refs\`
- [ ] **After merge — operator smoke**:
  - \`git pull\`, \`docker compose build --no-cache ingest\`, \`docker compose up -d --force-recreate ingest_migration ingest\`
  - Confirm \`Applying 0006_seed_prompt_v5_keyframes.sql (...)\` in migration logs.
  - Capture a new YouTube video → confirm the rendered doc contains either inline \`affine:image\` blocks (LLM followed prompt) or a \`## Keyframes\` appendix with all frames (fallback fired).

## Related

- Roadmap: [\`docs/plans/2026-05-12-video-frame-analysis-roadmap.md\`](docs/plans/2026-05-12-video-frame-analysis-roadmap.md) Tier 1
- Macro plan: [\`docs/plans/2026-05-12-video-frame-analysis-macro-plan.md\`](docs/plans/2026-05-12-video-frame-analysis-macro-plan.md) Phase 15
- Detailed plan: [\`docs/plans/2026-05-12-phase-15-keyframes-appear.md\`](docs/plans/2026-05-12-phase-15-keyframes-appear.md)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Verification checklist (engineer self-check before declaring done)

- [ ] Migration 0006 is idempotent (re-running migrations doesn't fail).
- [ ] `count_keyframe_refs()` returns a set of ints, handles `None` / empty input.
- [ ] When `body_md` has at least one `kf:N` ref, NO `## Keyframes` appendix appears.
- [ ] When `body_md` has zero `kf:N` refs and keyframes are available, the appendix appears with ALL available keyframes as `affine:image` blocks.
- [ ] When no keyframes are available (text-only article), no appendix.
- [ ] The appendix appears AFTER body_md but BEFORE the transcript section.
- [ ] Rerender endpoint produces the same appendix behaviour.
- [ ] Full test suite passes (~403 passed, 9 skipped).
- [ ] PR description includes the operator smoke checklist.
