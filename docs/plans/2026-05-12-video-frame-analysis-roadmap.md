# Video frame analysis — current state + roadmap

> **Status:** Diagnostic + forward-looking roadmap (not a single-phase
> implementation plan). Documents the current Phase 13 pipeline, identifies
> the gaps that surfaced after Phase 14 (templates), and proposes a tiered
> sequence of follow-up phases.

**Date:** 2026-05-12
**Spec it builds on:** [`2026-05-08-phase-13-video-frame-analysis.md`](2026-05-08-phase-13-video-frame-analysis.md)
**Code under discussion:** [`ingest/src/pipeline/video_analysis.py`](../../ingest/src/pipeline/video_analysis.py),
[`ingest/src/pipeline/extractors/cobalt_ext.py`](../../ingest/src/pipeline/extractors/cobalt_ext.py)

---

## 1. What's already shipped (Phase 13)

### 1.1 End-to-end flow

For every captured video that cobalt can download (YouTube, Vimeo,
TikTok, etc.):

```
URL
 ↓ cobalt_ext.extract()
 │
 ├── Audio path (primary) ──────────────────────────────────────────
 │     cobalt audio tunnel → mp3 → OpenAI Whisper API
 │     → transcript with [mm:ss] timestamp anchors
 │     → extracted.body_md
 │
 └── Video path (Phase 13) ─────────────────────────────────────────
       cobalt video tunnel → mp4 → video_analysis.analyze_video():
        1. PySceneDetect ContentDetector (threshold=27.0)
           → scene boundaries; or fallback fixed-interval (25/50/75%)
        2. ffmpeg single-frame extract per scene → JPEG files
        3. Pillow resize to 1024px long-edge
        4. Sonnet 4.6 vision call:
             input  = transcript + up to max_frames_per_video frames (def 12)
             output = narrative summary + per-frame caption + importance 0-10
        5. Filter: importance >= keyframe_importance_threshold (default 4)
        6. Cap at max_keyframes_in_doc (default 6)
        7. Upload kept frames to AFFiNE blobs via mcp_ext.upload_blob
        8. Return (video_summary, keyframes: list[KeyframeRef])
       →
       extracted.extra["video_summary"]      ← narrative grounded in audio+visual
       extracted.extra["keyframes"]          ← list of {timestamp_seconds, caption, blob_source_id}
       extracted.extra["video_analysis_ok"]  ← bool
```

### 1.2 How the template render sees it (Phase 14)

`templated_render._build_user_message()` and `chunked_render._reduce_to_templated_output()` both append two blocks to the LLM user message:

```text
Vision-grounded summary (transcript + keyframes):
<extracted.extra["video_summary"]>

Available keyframes (reference inline via `![caption](kf:<n>)`):
  [0] t=42.3s — IDE showing the React component
  [1] t=154.0s — Network panel with 200 OK
  [2] t=234.7s — Final result on screen
  ...
```

If the template's `system_prompt` instructs the LLM to use `kf:N` refs,
the output `body_md` contains `![cap](kf:N)` which the markdown renderer
resolves to inline `affine:image` blocks backed by the uploaded blobs.

### 1.3 What works today

- Whisper transcripts arrive with timestamp anchors.
- Scene-cut detection runs reliably on cobalt-downloaded videos.
- Vision call returns useful per-frame importance + captions.
- Keyframes are uploaded as AFFiNE blobs and referenced by `sourceId`.
- The render layer (`markdown_render.markdown_to_blocks`) correctly
  converts `![cap](kf:N)` → `affine:image` block with the right blob ref.
- `extracted_snapshot` preserves `keyframes` in JSONB so rerender keeps
  them available without re-downloading the video.

---

## 2. Gaps observed in production

### 2.1 Keyframes rarely show up in rendered docs

**Root cause.** The seed template's `system_prompt` does not actively
instruct the LLM to use `kf:N` references. It just describes the syntax
as "supported". The LLM treats them as optional decoration and almost
never emits them.

**Effect.** Vision analysis runs (Sonnet 4.6 call costs ~$0.05 per
video), keyframes are uploaded as blobs, but the rendered AFFiNE doc
contains zero inline frame images. Cost without benefit.

### 2.2 Keyframe selection is template-agnostic

**Root cause.** `analyze_video()` uses a single fixed importance
threshold (default 4) regardless of what the content is. The vision
call's "importance" score is a generic "is this visually interesting"
rating — it doesn't know whether the doc is becoming a recipe (wants
the ingredients shot + final plate), a tutorial (wants IDE screenshots
+ result windows), or a documentary (wants the diagrams flashed on
screen).

**Effect.** A YouTube tutorial about React gets the speaker's face as
a top-scoring keyframe (the vision model thinks faces are important),
while the actual code-on-screen frame ranks lower. The user gets
useless screenshots.

### 2.3 No OCR — text in frames is invisible

**Root cause.** The vision call describes frames in natural language
("IDE showing React code") but doesn't transcribe the on-screen text.

**Effect.** When a slide flashes a key statistic, equation, or code
snippet, the transcript+caption doesn't capture it. The user can see
the image but can't search / copy it. For technical content (tutorials,
documentaries, conference talks) this is the most valuable signal.

### 2.4 No timeline integration

**Root cause.** Keyframes are presented to the template LLM as a flat
list at the end of the user message. The transcript itself is a
separate block above. The LLM has to manually correlate timestamps
between the two sources to figure out where each frame "belongs".

**Effect.** Even when the LLM does emit a `kf:N` ref, it tends to
cluster them at the top of body_md (under a generic `## Visuals`
section) rather than weaving them into the relevant passage. The
reader doesn't see the screenshot next to the matching transcript.

### 2.5 Map-reduce drops keyframes during the map step

**Root cause.** `chunked_render` splits a long transcript into N
chunks; each chunk's map call summarizes that chunk alone. Keyframes
are only passed to the final reduce call. So per-chunk summaries
have no visual context.

**Effect.** For a 30-minute video, the map step's `ChunkSummary` doesn't
mention what was on screen during that time window. The reduce step
has to re-correlate keyframe timestamps against the chunk-level
section headers it gets back — a layer of indirection that loses
information.

### 2.6 No frame-quality filter

**Root cause.** No pre-filter for black frames, intro/outro cards,
near-duplicate frames, or low-information frames. Every scene cut
ends up in the vision call.

**Effect.** Vision tokens wasted on uninformative frames; per-frame
importance scores polluted by junk.

### 2.7 `embed-html` is supported but never used

**Root cause.** The renderer accepts ```embed-html ... ``` fences →
`affine:embed-html` block (inline SVG / HTML cards). Templates could
ask the LLM to emit "a styled SVG chart summarizing the key
numbers in this video" but the seed prompt doesn't suggest it.

**Effect.** A capability that distinguishes AFFiNE from plain markdown
sits dormant. No video doc has rendered a generated chart yet.

### 2.8 Speaker diarization absent

**Root cause.** Whisper transcripts don't separate speakers.
Multi-speaker podcasts (interviews, panels) come out as one monolithic
stream of text.

**Effect.** "Host said X, guest replied Y" structure is lost; the
template can't render speakers as bold headers, can't put a
`> [!callout]` next to a quote attributed to a specific person.

---

## 3. Proposed roadmap (tiered)

Each tier is roughly one phase. Higher tiers depend on lower ones.
Estimates are rough — buffer 30% for the unexpected.

### Tier 1 — Make keyframes actually appear (P1, ~2-4 hours)

**Why first.** Today's pipeline burns money on Sonnet vision calls but
produces zero visible keyframes in 95% of rendered docs. This tier
recoups that cost without changing any architecture.

**Two options, pick one:**

**Option A — Stronger seed-prompt instruction (cheap).**
- Migration 0006 updates the `(*, *)` seed prompt with explicit
  "when a section corresponds to a visible keyframe, embed it inline
  via `![caption](kf:N)`. Aim to embed 2-4 keyframes per video doc
  unless none are relevant." rule.
- Synthesized templates that already exist won't auto-update — user
  triggers re-synth or edits manually.
- Cost: 0 dev hours. Risk: LLM still ignores it.

**Option B — Orchestrator fallback "Keyframes" section (deterministic).**
- After body_md renders, if `rendered.body_md` references zero `kf:N`
  out of N available keyframes, the orchestrator appends a
  `## Keyframes` h2 + image blocks for the top 3 keyframes by importance.
- Templates that DO embed keyframes inline → skip the appendix.
- Cost: ~2 hours. Trade-off: the user always sees keyframes (good) but
  they're decoupled from the transcript context (less good than inline).

**Recommendation.** Do BOTH. Option A is the long-term ideal; Option B
is the safety net. Together they guarantee the user sees keyframes
while we tune the prompt over time.

**Tests.**
- `test_orchestrator_appends_keyframes_appendix_when_template_uses_none`
- `test_orchestrator_skips_keyframes_appendix_when_template_references_them`
- Updated seed-prompt test (substring check for "kf:N").

---

### Tier 2 — Template-aware keyframe selection (P2, ~6-10 hours)

**Goal.** The vision call knows which template the capture will render
under, and re-ranks frame importance for that template's purpose.

**Mechanism.**
1. Orchestrator resolves the template BEFORE invoking the extractor (or
   passes the template to the extractor for video captures specifically).
2. `cobalt_ext._maybe_run_video_analysis()` forwards
   `(template.name, template.system_prompt)` to `analyze_video()`.
3. `analyze_video()`'s vision-call system prompt gains a "purpose"
   block: *"This video will be rendered as a [template.name]. Frames
   important for that purpose: [extracted from system_prompt or
   generator_meta]. Score `importance` 0-10 with that purpose in mind."*
4. Keyframes returned still go through the existing
   `importance_threshold` filter, but now the scores reflect template
   purpose.

**Sequencing complication.** Today the flow is:
```
extract → classify → resolve_template → render
```
For Tier 2 we need:
```
                                ┌── template ──┐
extract → classify → resolve_template → render
              ↓ (back-prop into extract for this video)
```

Two options:
- **(a) Cheap:** run a lightweight pre-classification (just on URL +
  description + Whisper transcript first sentence) before extraction.
  Use that to resolve a candidate template. Pass it down to vision call.
- **(b) Two-pass:** extract → classify → resolve_template → IF VIDEO,
  re-run vision call with template-aware prompt → render. Adds one
  extra Sonnet call per video capture (~$0.05).

**Recommendation.** Option (b). The two-pass is honest about what
we're doing and easier to reason about. Cost is fine — we already pay
for one Sonnet vision call; the second runs against the same uploaded
frames (no re-extraction).

**Tests.**
- Mock Sonnet vision call; verify the system prompt includes
  template-specific purpose language.
- Verify keyframe re-ranking changes the order vs. a generic prompt.
- Integration: a YouTube/Tutorials capture re-ranks IDE screenshots
  above speaker face frames.

---

### Tier 3 — OCR on text-rich frames (P3, ~6-8 hours)

**Goal.** When a frame contains slide text, code on screen, or chart
data, transcribe it and make it part of the keyframe metadata.

**Mechanism.**
1. After Sonnet vision returns captions + importance, inspect each
   caption for OCR signals: keywords like "slide", "code", "diagram",
   "chart", "text on screen", "title card".
2. For matching frames, run an OCR pass. Options:
   - **Local:** Tesseract or PaddleOCR. Pros: free. Cons: another C
     dependency in the Docker image.
   - **Cloud:** Anthropic vision call with a "transcribe text in this
     image" prompt. Pros: zero new deps, same model. Cons: ~$0.01 per
     frame.
3. Store OCR'd text on the keyframe metadata:
   ```python
   class KeyframeRef:
       blob_source_id: str
       caption: str
       timestamp_seconds: float
       ocr_text: str | None  # NEW
   ```
4. Surface OCR'd text in the template render user message:
   ```
   Available keyframes:
     [0] t=42.3s — IDE with React code
         OCR: "function useEffect(callback, deps) { ... }"
     [1] t=154.0s — Slide titled "Why AI struggles with Swift"
         OCR: "1. Data Gap  2. API Drift  3. Benchmarking Bias"
   ```
5. Template can quote the OCR'd content directly in body_md (now even
   if the LLM doesn't embed the image, it has the SUBSTANCE of the
   slide as text).

**Migration impact.** `extracted_snapshot.keyframes` JSONB shape gains
the `ocr_text` field. Backwards compatible — older snapshots have null.

**Recommendation.** Start with the cloud OCR variant (Anthropic
vision). One less dependency, simpler. Switch to local Tesseract if
cost becomes a problem.

**Tests.**
- Mock OCR; verify caption keyword detection triggers OCR call.
- Verify OCR text reaches the template user message.
- Integration: a tutorial video with code-on-screen frames produces
  body_md that quotes the code snippet.

---

### Tier 4 — Timeline-aware integration (P4, ~10-16 hours)

**Goal.** Keyframes appear INLINE in body_md next to the relevant
transcript passage, not just as a flat list at the top.

**Mechanism.**

For the single-call render path (`templated_render`):
- Keyframes are already in the user message. Update the seed prompt:
  *"Each keyframe has a timestamp. The transcript also has timestamps
  (e.g. `[0:42]` anchors). When you discuss a transcript passage near a
  keyframe's timestamp, embed the keyframe inline via `![cap](kf:N)`
  immediately after the relevant paragraph."*
- Provide a worked example in the prompt so the LLM mimics the pattern.

For the map-reduce path (`chunked_render`):
- The chunker splits the transcript by timestamp boundaries. For each
  chunk, **filter the keyframes** to only those whose
  `timestamp_seconds` falls within the chunk's time range.
- Pass the filtered keyframes to the chunk's map call.
- The map call's `ChunkSummary` gains a new field:
  `keyframe_refs: list[str]` — markdown `![cap](kf:N)` references the
  chunk wants to embed in its section. Indices stay global (so the
  reducer can resolve them).
- The reducer's user message includes these per-chunk keyframe lists
  inside the chunk digest section.
- Reducer instruction: *"Each section's keyframe_refs should be
  preserved in that section of your body_md."*

**Schema additions.**
```python
class ChunkSummary(BaseModel):
    section_title: str
    timestamp_range: str | None
    key_points: list[str]
    notable_quotes: list[str]
    references: list[str]
    reveal: str | None
    keyframe_refs: list[str]  # NEW: e.g. ["kf:2", "kf:4"]
```

**Tests.**
- Chunker: verify `timestamp_seconds` range comparison correctly assigns
  keyframes to chunks.
- Map step: verify `keyframe_refs` gets populated when the chunk's
  prompt references a frame.
- Reducer: verify per-chunk keyframe refs survive into the final
  `body_md`.

**Migration impact.** `ChunkSummary` shape is additive — no schema
migration needed. But the `chunked_render` user message format changes,
so any cached prompt-cache prefix gets invalidated on deploy.

---

### Tier 5 — Frame-quality pre-filter (P5, ~3-5 hours)

**Goal.** Skip the vision call entirely for frames that obviously have
no information.

**Filters.**

1. **Blackness.** Compute `np.mean(frame)`; if average pixel value
   < some threshold (e.g. 20 on 0-255 scale), the frame is mostly black
   (intro fade, transition). Drop.

2. **Perceptual hash dedup.** Use `imagehash.phash` to compute a 64-bit
   hash per frame. If two frames within a video have hamming distance
   ≤ 5, treat them as duplicates and keep only the first by timestamp.
   Common case: video has the same title-card frame at 0:00 and the
   exact same one at 0:15 after the intro animation.

3. **Low-entropy.** Compute Shannon entropy of the frame's grayscale
   histogram. Below a threshold (e.g. 4 bits) means the frame is
   uniform (blank slide, single color). Drop.

**Output.** The filter runs BETWEEN `_detect_and_extract_frames()` and
`_vision_call()`. Vision call gets a smaller list — fewer tokens, less
cost, better importance signal.

**Caveats.** Perceptual hash needs the `imagehash` library (one new
Python dep, pure Python). Blackness + entropy use numpy which is
already in deps.

**Tests.**
- Synthesize black/uniform/duplicate test images. Verify each filter
  triggers.
- Verify filter doesn't drop genuinely distinct content frames.

---

### Tier 6 — `embed-html` symbolic visualizations (P6, ~8-12 hours)

**Goal.** Templates can ask the LLM to generate inline SVG charts /
styled cards from transcript data. Showcases the dormant
`affine:embed-html` block.

**Mechanism.**

Specialized templates (e.g. `(youtube, Documentary)`,
`(arxiv, Theory)`) get system prompts that include:
*"When the source provides numerical data, statistics, or comparison
points (e.g. transistor counts, benchmark scores, percentages), emit
a `> ```embed-html\n<svg>...</svg>\n```` block summarizing the data
visually. Inline SVG only; no external dependencies. Use plain shapes
(rect, circle, text) — no external CSS / fonts."*

Sonnet 4.6 is capable of generating inline SVG charts. The renderer
already converts ```embed-html``` → `affine:embed-html` block.

**Risks.**
- SVG output can be malformed. Add a validation step that parses the
  SVG and rejects on parse fail (falls back to dropping the block).
- Cost: an SVG-generating template call uses ~2-3x the tokens of a
  text-only render.

**Migration impact.** No code changes outside specialized template
prompts. The seed `(*, *)` template should NOT enable this (cost would
balloon for all captures). Only opt-in via specialized templates.

**Tests.**
- Mock Sonnet returns an `embed-html` block in body_md; verify
  renderer produces `affine:embed-html`.
- SVG validation: malformed SVG → block dropped + warn log.

---

### Tier 7 — Speaker diarization (P7, ~12-20 hours)

**Goal.** Multi-speaker podcasts get "Host: ... / Guest: ..."
attribution in transcript and body_md.

**Mechanism.**

1. Switch from OpenAI Whisper to a transcript provider with
   diarization. Options:
   - `pyannote.audio` + local Whisper (free, heavy Docker image).
   - Anthropic doesn't offer diarization. AssemblyAI / Deepgram do
     (~$0.005-0.01 / minute) — cleaner than self-hosting.
2. Transcript format gains speaker labels:
   ```
   [SPEAKER_00] [0:00] So recently there was a trend on X...
   [SPEAKER_01] [0:42] Yeah, and what's interesting is...
   ```
3. The map-step prompt knows to preserve speaker labels in
   `notable_quotes` and `key_points`.
4. Template seed prompt teaches: *"For multi-speaker content, use
   bold prefixes for speaker names: **Host:** ... / **Guest:** ...
   Resolve `SPEAKER_00` etc. to real names if the source description
   provides them."*

**Risks.** Adding a new dep / provider is friction. Diarization
accuracy is variable for low-quality audio (typical podcast clips
from social media). Worth gating behind a `DIARIZATION_ENABLED=false`
flag initially.

**Recommendation.** Defer. Solve Tiers 1-3 first; revisit when
multi-speaker content becomes a substantial fraction of captures.

---

## 4. What I'd do this week

If picking one tier to ship now:

**Tier 1 (Option A + B) — ~3 hours.** Cheapest, fixes the most visible
bug (keyframes never appear), gives operators something concrete to
test. The migration is one SQL file; the orchestrator change is one
new helper + one if-branch.

Following sequence over the next 2-3 weeks:
- Week 1: Tier 1 (both options).
- Week 2: Tier 5 (frame-quality filter) — improves Tier 1's signal AND
  reduces Sonnet vision costs by ~30-40%. Cheap and standalone.
- Week 3: Tier 2 (template-aware re-ranking). The biggest quality lift
  for specialized templates. Cost: one extra Sonnet call per video,
  but the keyframes finally match what the template wants.
- Week 4+: Tier 3 (OCR) → Tier 4 (timeline integration). The
  combination unlocks "transcript-with-inline-screenshots-and-quoted-
  slide-text" which is the end-goal experience.

Tiers 6 and 7 are nice-to-haves; don't block on them.

---

## 5. Open questions

- **Cost ceiling.** What's the per-capture budget the user accepts?
  Current: ~$0.10-0.20 for a long video (Whisper + Sonnet vision +
  Sonnet text). Adding OCR + template-aware re-rank pushes to ~$0.30.
  Acceptable for self-hosted personal use; would be untenable for
  multi-tenant.
- **Storage.** Each kept keyframe is ~200-500 KB blob. At 100 videos /
  month × 6 keyframes each, that's ~250-700 MB/year of blob storage.
  AFFiNE handles this fine but worth monitoring.
- **Re-render cost.** When the user edits a template and rerenders an
  old video capture, vision analysis does NOT re-run (snapshot has the
  keyframes). But Tier 2's template-aware re-ranking WOULD need to
  re-run the vision call against the original frames. If frames aren't
  preserved on disk (currently they're temp files inside the cobalt
  workdir, deleted after extraction), this won't work. Tier 2
  implementation needs to decide: keep frame JPEGs as blobs forever,
  or accept that template-aware ranking only applies to fresh captures.
- **Privacy.** Whisper API processes user-captured video audio.
  Anthropic vision processes user-captured video frames. Both go
  off-host. Acceptable for current single-user setup; document in the
  privacy section of `README.md`.

---

## 6. What changed since the original Phase 13 spec

The original [`2026-05-08-phase-13-video-frame-analysis.md`](2026-05-08-phase-13-video-frame-analysis.md)
spec planned for:

| Spec promise | Actual state |
|---|---|
| Scene detect + frame extract + vision call | ✅ Shipped |
| Keyframes uploaded as blobs and rendered as `affine:image` | ✅ Shipped |
| `## Keyframes` section in the rendered doc | ❌ Removed in Phase 14 — keyframes became passive template inputs via `kf:N` refs. **The Tier 1 (B) item in this roadmap restores this as a fallback when templates don't reference any.** |
| Vision-grounded summary feeds the title generator | ✅ Shipped (lives in `extracted.extra["video_summary"]`, surfaced in render user messages) |
| Template-aware keyframe re-ranking | ❌ Documented as out-of-scope v1; this roadmap proposes it as Tier 2 |
| OCR'd slide text | ❌ Never specified; this roadmap adds it as Tier 3 |
| Inline keyframes at transcript timestamps | ❌ Never specified; Tier 4 |

The Phase 13 spec was correct for what it set out to do. Phase 14
templates revealed that the "use keyframes when relevant" decision is
better made by the template's LLM call than by a hardcoded `##
Keyframes` section — but the LLM needs more instruction (Tier 1) and
better context (Tiers 2-4) to actually exercise that decision well.

---

## 7. Decision request

Tell me which tier(s) you want to ship next and I'll write the
focused implementation plan for that single tier. Most likely
order, based on cost-vs-impact: **Tier 1 → Tier 5 → Tier 2 → Tier 3 →
Tier 4 → (Tier 6) → (Tier 7).**
