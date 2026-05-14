# Phase 18 — Transcript-guided keyframe selection

**Date:** 2026-05-14
**Builds on:**
- [`2026-05-08-phase-13-video-frame-analysis.md`](2026-05-08-phase-13-video-frame-analysis.md) — original pipeline.
- [`2026-05-12-video-frame-analysis-roadmap.md`](2026-05-12-video-frame-analysis-roadmap.md) — Tier 2 (template-aware re-ranking) is the closest predecessor; this phase is a stronger version of the same insight.
- [`2026-05-14-phase-17-transnetv2-detector.md`](2026-05-14-phase-17-transnetv2-detector.md) — composes with this phase; TransNetV2 picks WHERE shots are, transcript ranking picks WHICH of those shots matter.

---

## 1. Goal

Use speech itself — the cheapest, highest-information signal we already
collect — to drive WHICH scene-cut candidate timestamps survive into the
vision call. The diagnostic in the May-12 roadmap (§2.1–2.4) said that
keyframes rarely appear in rendered docs because the importance signal
arrives AFTER we've already spent the vision-call budget. This phase
inverts the order: rank speech windows by deictic/visual cues FIRST,
then only extract frames from the high-scoring windows.

**Concretely:**
1. **Phase 1 (already exists)** — Whisper / YT-captions produce a
   timestamped transcript.
2. **Phase 2 (NEW)** — single text-only Claude call ranks each ~45-second
   window for `(importance, visual_anchor_likelihood)`.
3. **Phase 3 (modified)** — scene-detect runs as before, then candidate
   timestamps in low-anchor windows are dropped. Each surviving frame
   picks up the speech window that motivated it as a `motivation` string.
4. **Phase 4 (already exists, now sharper)** — the vision call sees per-frame
   motivation (`"Spoken nearby: as you can see in this chart"`), so its
   importance ratings are anchored in what the speaker was actually
   doing at that moment.

**Non-goals:**
- No replacement of the existing scene-cut detectors. They still produce
  the raw candidate list — the new ranking is a filter on top.
- No re-engineering of the vision call's structured output schema.
- No template-aware ranking yet (Tier 2 from the May-12 roadmap). That
  is a clean follow-up — feed the template's `system_prompt` into the
  ranker's prompt so importance is template-relative.
- No new external model dependencies. All scoring uses the existing
  Claude API access we already have for the vision call.

## 2. Scope

| File | Change |
|---|---|
| `ingest/src/pipeline/extractors/ytdlp_ext.py` | `_whisper_transcribe` now uses `response_format="verbose_json"` and returns `(text, segments)` — the timestamps it always had but discarded are now preserved. |
| `ingest/src/pipeline/extractors/cobalt_ext.py` | Thread `whisper_segments` from `_whisper_transcribe` into `_maybe_run_video_analysis` → `analyze_video`. For the YT-captions path, `whisper_segments=[]` (parser falls back to the markdown anchors `[**M:SS**](...&t=Ns)` already embedded in those transcripts). |
| `ingest/src/pipeline/video_analysis.py` | New `_TranscriptSegment` / `_RankedWindow` dataclasses; new `_build_transcript_segments`, `_coalesce_segments_for_ranking`, `_rank_transcript_segments`, `_filter_frames_by_ranking`, `_window_for_timestamp`, `_summarize_window_text`. `_ExtractedFrame` gains a `motivation: str` field (defaults to `""` — backwards compatible). `analyze_video` accepts optional `whisper_segments` kwarg and runs the ranking step between scene-detect and vision call. |
| `ingest/src/config.py` | Six new env knobs (enable flag, threshold, reserve ratio, min-words, window-seconds, window-chars, max-windows) — all with sane defaults. |
| `ingest/tests/test_video_analysis.py` | 13 new tests covering: YT anchor regex, segment-builder priority, word-count threshold, coalescing, window lookup, candidate filter (drop / reserve / fallback paths), end-to-end ranking + motivation propagation, disabled-flag bypass, no-segments bypass. |
| `ingest/tests/test_extractor_cobalt.py` / `test_extractor_ytdlp.py` | Existing `_whisper_transcribe` mocks updated to return the new `(text, segments)` tuple. No semantic test change — same body assertions. |

No migrations. No new dependencies. No new Docker layers.

## 3. Design notes

### 3.1 The two scores and why they're separate

A passage's _importance_ ("does this passage matter to a reader?") and
its _visual_anchor_ score ("is there a visual on screen here?") answer
different questions:

- **Important + anchored** → keep (top priority). Speaker is explaining
  a key insight with a visual.
- **Important + NOT anchored** → speech matters, but no frame would add
  signal. Don't waste a keyframe slot. The transcript already carries
  the value into body_md.
- **NOT important + anchored** → speaker pointed at something tangential.
  Skip unless we have spare budget.
- **NOT important + NOT anchored** → drop.

The filter therefore _primarily_ sorts by `visual_anchor` with
`importance` as a tiebreaker — this is what makes the right thing happen
for talking-head educators (low visual_anchor everywhere → small keyframe
count rather than forcing useless screenshots).

### 3.2 The B-roll safety net (`pure_visual_reserve_ratio`)

A documentary with strong voiceover + unmentioned imagery would lose all
its frames under a pure visual-anchor filter. The reserve quota — 20% of
the keyframe budget by default — fills with the highest-IMPORTANCE
frames regardless of visual_anchor. So a chart that the speaker never
addresses still has a shot at survival _if_ its surrounding speech
window scored high on importance.

The reserve is configurable to 0.0 for users who want strict
speech-anchored selection (screencasts, tutorials where unmentioned
content is mostly noise).

### 3.3 Why Whisper `verbose_json` instead of proportional chunking

Proportional chunking — chop the transcript into N equal text-length
windows and assume each covers `(N/total) × duration` seconds — sounded
attractive (no API-shape change) but breaks on real content:

- A 30s intro-music section produces zero transcript; mapping text
  position 0% to time 0% would put the speaker's "look at this" at the
  wrong timestamp.
- Speakers vary in cadence — a fast section has more words per second
  than a slow one.

Whisper already returns exact segment timestamps when asked
(`response_format="verbose_json"`, `timestamp_granularities=["segment"]`)
at no extra cost. The change is small and a clear win.

### 3.4 Window coalescing

Whisper segments are 5-10 seconds wide — too fine-grained to rank
usefully ("does this 8 seconds contain an infograph?" is too local a
question). The `_coalesce_segments_for_ranking` helper merges consecutive
segments until the cumulative span hits `transcript_ranking_window_seconds`
(default 45s), giving the LLM windows wide enough to spot deictic
markers and topic pivots.

### 3.5 What the ranking call costs

One Claude call per video.
- Input: ~80 windows × ~600 chars each = ~50K characters ≈ 12K tokens.
- Output: 80 small JSON objects, ~2K tokens.
- ~$0.005 per video at current Sonnet 4.6 rates.

This is a rounding error against the ~$0.10-$0.20/video we already pay,
and the savings should be larger than that — the vision call sees fewer
frames (vision tokens scale linearly with image count).

### 3.6 Backwards compatibility

- `_whisper_transcribe` signature changed (`str` → `(str, list[dict])`).
  All in-repo callers updated. External callers don't exist (it's a
  private helper).
- `_ExtractedFrame.motivation` defaults to `""` — old code paths and
  any test that builds frames manually keep working.
- `analyze_video`'s new `whisper_segments` kwarg has a `None` default —
  any caller that ignores it gets the old behavior (with one quiet
  improvement: if the transcript happens to have YT-style anchors, the
  parser still picks those up and runs ranking).
- Feature can be disabled wholesale via
  `TRANSCRIPT_GUIDED_SELECTION_ENABLED=false`.

## 4. Test strategy

All tests are mock-driven — no real Whisper / Claude / video files needed.

| # | Test | Purpose |
|---|---|---|
| 1 | `test_yt_anchor_regex_parses_mm_ss_and_hh_mm_ss_links` | The regex catches both `[**0:42**]` and `[**1:02:03**]` shapes. |
| 2 | `test_build_transcript_segments_prefers_whisper_segments` | Whisper wins over anchor parsing. |
| 3 | `test_build_transcript_segments_parses_youtube_anchors_when_no_whisper` | YT-captions path recovers segments from markdown anchors. |
| 4 | `test_build_transcript_segments_returns_empty_on_no_signal` | Plain transcript with no anchors → empty list → ranking bypassed. |
| 5 | `test_has_enough_words_for_ranking_threshold` | Tiny transcripts skip ranking. |
| 6 | `test_coalesce_segments_merges_into_window_seconds` | Whisper's 5-10s segments roll up to ~45s windows. |
| 7 | `test_window_for_timestamp_locates_covering_window` | Timestamp → window lookup, edge cases included. |
| 8 | `test_filter_frames_by_ranking_drops_low_anchor_keeps_high` | Primary filter behavior. |
| 9 | `test_filter_frames_by_ranking_reserves_quota_for_high_importance_unanchored` | B-roll safety net works. |
| 10 | `test_filter_frames_by_ranking_fallback_when_no_anchored_frames` | Never return empty: importance-only fallback. |
| 11 | `test_analyze_video_skips_ranking_when_disabled` | `TRANSCRIPT_GUIDED_SELECTION_ENABLED=false` honored. |
| 12 | `test_analyze_video_calls_ranking_and_attaches_motivation` | End-to-end happy path: ranking fires, surviving frame carries motivation, motivation reaches vision call. |
| 13 | `test_analyze_video_skips_ranking_when_no_segments` | Plain Whisper without segments AND no anchors → bypass. |

## 5. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Ranking misses an actual visual-anchored moment | The 20% importance-only reserve catches high-importance frames the speaker didn't explicitly point at. |
| Empty transcript / music video | `_has_enough_words_for_ranking` skips the ranking call entirely below 50 words. |
| Ranking call fails (rate limit, network) | Exception swallowed inside `analyze_video`; falls through to the pre-Phase-18 candidate path. |
| Visual_anchor threshold too strict for a corpus | Operator tunes `TRANSCRIPT_VISUAL_ANCHOR_THRESHOLD` (default 4); lower = more permissive. |
| Whisper `verbose_json` not supported by the configured Whisper endpoint | The new `_whisper_transcribe` falls back gracefully — `segments=[]` and downstream behaves as pre-Phase-18. |
| Long-form video (>60 min) | `transcript_ranking_max_windows=80` caps the ranking input so the call stays predictable. |

## 6. Verification before merge

- `pytest tests/test_video_analysis.py -v` → 34/34 tests pass.
- `pytest tests/test_video_analysis.py tests/test_video_analysis_filters.py tests/test_extracted.py tests/test_classifier.py tests/test_classification_model.py tests/test_logging_setup.py -q` → 78/78 pass.
- Module import smoke check: `from src.pipeline.video_analysis import analyze_video, _filter_frames_by_ranking` → succeeds.
- Default behavior unchanged when `TRANSCRIPT_GUIDED_SELECTION_ENABLED=false`.

## 7. What this composes with

- **Phase 16 quality pre-filter** — runs AFTER scene-detect, BEFORE
  ranking. Removes obviously useless frames cheaply; the ranking call
  doesn't waste tokens scoring intro/outro cards.
- **Phase 17 TransNetV2** — better detector → fewer junk candidates →
  less work for the ranking filter to do. The two are independent and
  compose multiplicatively.
- **May-12 Tier 2 (template-aware re-ranking)** — once shipped, the
  template's `system_prompt` can feed into the ranker's prompt so
  importance is template-relative ("score for a Recipe doc" vs "score
  for a Tutorial doc").
- **May-14 Stage B (self-hosted VLM)** — fewer frames per video means
  the local GPU isn't a bottleneck even on modest hardware.

## 8. Follow-ups

- **Template-aware ranking.** Currently the ranker uses a generic
  prompt. Feed the resolved template's name + `system_prompt` into
  `_RANKING_SYSTEM_PROMPT` so a recipe-template ranks
  "ingredients-on-screen" highest, a tutorial-template ranks
  "code-on-screen" highest, etc.
- **Re-rank on rerender.** Today the ranking runs once at capture time;
  a user editing the template later doesn't re-rank existing frames.
  Cheap to fix when the orchestrator gains a "rerender" mode.
- **Local LLM for the ranker.** Once Stage B of the acceleration
  roadmap ships, the ranker could move to the self-hosted VLM too —
  same prompt, free per-call. Defer until Stage B is shipped.
