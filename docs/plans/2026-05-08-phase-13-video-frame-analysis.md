# Phase 13: Video frame analysis — extract key screenshots + Claude vision summary

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Beyond the audio transcript, extract a handful of *visually* important frames from each captured video and use Claude's vision capability to (a) generate accurate summaries grounded in what's actually shown, not just what's said, and (b) embed the keyframes directly into the AFFiNE doc as image blocks. This solves the "AI hallucinates summary because it has no captions and no visual context" problem the cobalt fallback hits today.

**Architecture:** New extractor pipeline step `video_analysis` runs after audio extraction. Downloads the video file (cobalt with `downloadMode=auto`), uses [PySceneDetect](https://github.com/Breakthrough/PySceneDetect) to find scene-change frames (typically 5-15 per video), then sends those frames + transcript to Claude Sonnet 4.6 (vision-capable) in a single multimodal call. Sonnet returns a structured response: which frames are most informative + a caption for each + the grounded video summary. Frames get uploaded to AFFiNE as blob attachments via mcp-ext, then embedded as image blocks at the right spots in the doc body.

**Tech Stack:** [PySceneDetect 0.6+](https://www.scenedetect.com/) (`pip install scenedetect[opencv]`), ffmpeg (already available — yt-dlp dependency), Anthropic vision API (Sonnet 4.6 multimodal), mcp-ext blob upload (need to add `upload_blob` tool — check if already there), Pillow for re-encoding extracted frames at sensible resolution before sending to Claude.

**Why now:** The Phase 12 cookie-sync fixes the *audio* path for YouTube. But:
- Many YT videos have no captions and audio with minimal speech (music videos, gameplay, design reels)
- Image-heavy content (recipes, tutorials, product reviews) loses 80% of its value with audio-only
- Today's pipeline hallucinates summaries when transcript is empty or sparse

Visual grounding is the next quality leap.

---

## File Structure

| File | Responsibility |
|---|---|
| `ingest/pyproject.toml` | Add `scenedetect[opencv]>=0.6.4` + `Pillow>=10`. |
| `ingest/src/pipeline/video_analysis.py` | NEW. Orchestrates scene detection + frame extraction + Claude vision call. |
| `ingest/src/pipeline/extractors/_video_download.py` | NEW. Cobalt video download (vs. audio-only). Mirrors `cobalt_ext._download_audio` but for full video stream. |
| `ingest/src/pipeline/extractors/cobalt_ext.py` | Wire video_analysis into the happy path: extract → audio (transcript) → video frames (vision) → compose body. |
| `ingest/src/pipeline/orchestrator.py` | Pass extracted frames + captions through to the doc-block builder. |
| `ingest/src/mcp_client.py` | Add `upload_blob(file_path) → blob_id`. |
| `ingest/src/config.py` | New settings: `video_analysis_enabled`, `vision_model` (default `claude-sonnet-4-6`), `max_frames_per_video` (default 8), `frame_resolution` (default 1024). |
| `mcp-ext/src/blob-tools.ts` | NEW or extend. Add `upload_blob` MCP tool — accepts base64 + content-type, writes to AFFiNE blob storage, returns blob_id. |
| `ingest/src/pipeline/orchestrator.py` | Insert image blocks (`{type: 'image', sourceId: blobId}`) into body where Claude says they belong. |
| `ingest/tests/test_video_analysis.py` | NEW. Mock PySceneDetect + vision API; test scene→frame→caption flow. |

---

## Task 1: Video download path

**Files:**
- Create: `ingest/src/pipeline/extractors/_video_download.py`
- Modify: `ingest/src/config.py` (add `cobalt_video_max_size_mb` cap, default 200)

- [ ] **Step 1: cobalt video tunnel request**

Mirror `cobalt_ext._request_tunnel` but with `downloadMode: "auto"` (cobalt's video+audio-merged path). Payload:
```json
{"url": "<target>", "downloadMode": "auto", "videoQuality": "720"}
```
720p is the sweet spot — high enough to read text in tutorials, low enough to keep file sizes manageable.

- [ ] **Step 2: streaming download with size cap**

Stream the response body, abort if total bytes exceed `settings.cobalt_video_max_size_mb * 1024 * 1024`. Long-form content > 200MB is excluded from frame analysis (audio-only path stays).

- [ ] **Step 3: deterministic filename**

Output to `{workdir}/video.mp4`. Same workdir as cobalt_ext audio so cleanup is unified.

---

## Task 2: Scene detection + frame extraction

**Files:**
- Create: `ingest/src/pipeline/video_analysis.py` (scene detection block)

- [ ] **Step 1: PySceneDetect content-aware detection**

```python
from scenedetect import detect, ContentDetector

def find_scene_frames(video_path: Path, *, max_frames: int = 8) -> list[FrameInfo]:
    scenes = detect(str(video_path), ContentDetector(threshold=27.0))
    # Take first frame of each scene; cap at max_frames
    ...
```

Threshold 27 is PySceneDetect's recommended default for general content. For shorts (<60s), drop to 15 to catch faster cuts.

- [ ] **Step 2: Extract frames via ffmpeg**

For each scene's start timestamp, run ffmpeg seek + single-frame extraction:
```
ffmpeg -ss {ts} -i {video} -frames:v 1 -q:v 2 {workdir}/frame-{idx}.jpg
```
JPEG quality 2 = visually lossless, ~80% size reduction vs PNG.

- [ ] **Step 3: Resize to vision-API-friendly dimensions**

Pillow resize to longest edge = 1024px (Sonnet 4.6 vision sweet spot — Anthropic recommends ~1.15 megapixels). Save as JPEG q=85.

---

## Task 3: Claude vision call — caption + summary

**Files:**
- Modify: `ingest/src/pipeline/video_analysis.py`

- [ ] **Step 1: Compose multimodal message**

Single `messages.parse(output_format=...)` call:

```python
class FrameCaption(BaseModel):
    frame_index: int
    caption: str
    importance: int  # 0-10, how essential to understanding the video

class VisionAnalysis(BaseModel):
    summary: str  # 3-5 sentences, grounded in BOTH audio + visuals
    keyframes: list[FrameCaption]  # sorted by importance desc
```

System prompt: "You analyze short videos. Given the audio transcript + N keyframes, write a grounded summary AND caption each frame. Mark importance 0-10 — frames showing distinctive content (UI screens, code, recipe steps, faces, charts) get high scores; transition / loading / black frames get 0."

- [ ] **Step 2: Pack frames as base64 image blocks**

```python
content = [
    {"type": "text", "text": f"Audio transcript:\n\n{transcript}\n\nFrames follow."},
    *[
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}
        for b64 in frame_b64s
    ],
    {"type": "text", "text": "Return strict JSON per the schema."},
]
```

- [ ] **Step 3: Filter to top-N keyframes**

Drop any frame with `importance < 4`. Keep at most `settings.max_keyframes_in_doc` (default 4) for the final doc — the rest are noise. Order by timestamp ascending, not importance, so they read in chronological order.

---

## Task 4: Upload frames to AFFiNE as blobs

**Files:**
- Modify: `mcp-ext/src/server.ts` (register new tool)
- Create: `mcp-ext/src/blob-tools.ts`

- [ ] **Step 1: AFFiNE blob upload via GraphQL**

AFFiNE exposes `setBlob` mutation that accepts a base64-encoded file + workspace id. Returns the blob hash, which becomes the `sourceId` for image blocks.

- [ ] **Step 2: New MCP tool `upload_blob`**

```ts
const uploadBlob: ToolDefinition = {
  name: 'upload_blob',
  description: 'Upload a binary file (image, audio, video) to the workspace blob storage. Returns the sourceId to reference in image / attachment blocks.',
  inputSchema: {
    type: 'object',
    properties: {
      contentType: { type: 'string', description: 'MIME type, e.g. image/jpeg' },
      base64: { type: 'string', description: 'base64-encoded bytes' },
      filename: { type: 'string', description: 'Original filename for display' },
    },
    required: ['contentType', 'base64'],
  },
  async handler(token, args) { ... },
};
```

- [ ] **Step 3: Python client wrapper**

`MCPClient.upload_blob(content_type, base64_data, filename) → {sourceId}`

---

## Task 5: Embed image blocks in doc body

**Files:**
- Modify: `ingest/src/pipeline/orchestrator.py` (`_build_body_blocks`)

- [ ] **Step 1: Image block spec**

AFFiNE image block (from `mcp-ext/src/block-builder.ts`):
```ts
{ type: 'affine:image', prop:sourceId: '<blob-hash>', prop:caption: '<text>' }
```

Need to add to `BlockSpec` union and `addBlockFromSpec` if not already supported.

- [ ] **Step 2: Compose body with frames interleaved**

New layout when video_analysis succeeded:
```
## Summary
{summary from vision call — grounded in both audio + visuals}

## Description (if from yt-dlp)
{description}

## Keyframes
[image: frame-1.jpg]
{caption-1}
[image: frame-2.jpg]
{caption-2}
...

## Transcript
{transcript}

Source: {url}
```

- [ ] **Step 3: Graceful degradation**

If video_analysis is disabled OR fails (no video downloaded, scenedetect crashed, vision call returned empty), fall back to today's text-only body. The whole feature is best-effort.

---

## Task 6: Wire into cobalt_ext happy path

**Files:**
- Modify: `ingest/src/pipeline/extractors/cobalt_ext.py`

- [ ] **Step 1: Optional video download**

After audio download succeeds, if `settings.video_analysis_enabled`:
1. Try to download video (Task 1)
2. Run scene detection + frame extraction (Task 2)
3. Run Claude vision (Task 3)
4. Upload frames + attach to Extracted.extra

If any of these fails, log + continue with audio-only.

- [ ] **Step 2: Pass frames + captions through Extracted**

Add `keyframes: list[KeyframeRef] | None` to `Extracted.extra`:
```python
@dataclass
class KeyframeRef:
    blob_source_id: str
    caption: str
    timestamp_seconds: float
```

Orchestrator reads these in `_build_body_blocks` and emits image blocks.

---

## Task 7: Tests

**Files:**
- Create: `ingest/tests/test_video_analysis.py`
- Modify: `ingest/tests/test_extractor_cobalt.py` (add video-analysis-enabled path)

- [ ] **Scene detection produces expected frame count**: mock ContentDetector to return N scenes, verify N frames extracted (capped at max_frames).
- [ ] **Vision call gets the right shape**: mock `messages.parse`, assert message content has 1 text block + N image blocks + 1 closing text.
- [ ] **Frames < importance threshold dropped**: vision returns 8 frames, only 4 with importance ≥ 4 → only 4 in final body.
- [ ] **Blob upload integration**: MCPClient.upload_blob called once per kept frame.
- [ ] **Happy path end-to-end** (with all mocks): cobalt_ext.extract returns Extracted with `extra.keyframes` populated.
- [ ] **Disabled by config**: `video_analysis_enabled=False` → no video download, no vision call, body unchanged.
- [ ] **Graceful degradation**: video download throws → audio-only path still produces a doc.

---

## Out of scope for v1

- **Frame deduplication via perceptual hashing.** Two scenes that look identical (e.g. talking-head shots back-to-back) might both clear the threshold. Could add `imagehash`-based dedup — defer until we see this in practice.
- **GIF output of full motion.** Static keyframes only. Animation extraction would need a separate pipeline.
- **Audio-track scene detection.** PySceneDetect supports it, but content-aware is more reliable for our use case.
- **Per-platform tuning.** Same scene-detect threshold for IG reels (60s, fast cuts) as YT long-form (10min, slower cuts). Probably need per-platform thresholds eventually; ship a single default first.
- **Video-only summarization** (no audio). When transcript is empty AND keyframes show no clear content, fall back to "(no useful content extracted)" rather than synthesizing. Will need a confidence floor on the vision response.

---

## Risk assessment

| Risk | Likelihood | Mitigation |
|---|---|---|
| Vision API costs balloon on long videos | Medium | Cap `max_frames_per_video` at 8 by default; resize to 1024px so each frame is ~50KB; ~$0.003 per video on Sonnet 4.6 |
| 200MB video download fills tmpfs | Low | Hard cap, ffmpeg cleanup in `finally`. Tmpfs is 2GB, plenty of headroom |
| Scene detection fails on short videos (<5s) | Medium | If detector returns 0 scenes, sample at fixed timestamps (25%, 50%, 75% of duration) |
| AFFiNE blob storage limits | Low | Workspace settings already cap blob size; we send small JPEGs (~50KB each, max ~400KB per video) |
| Claude vision hallucination | Medium | System prompt requires `frame_index` references; if Claude captions a frame number we didn't send, drop the response and log |
| Cobalt video download blocked (YT, age-gate) | High | Phase 12 cookies should help. If still blocked, fall back to audio-only as today — feature gracefully degrades |

---

## Acceptance criteria

- [ ] `video_analysis_enabled=True` (default) — capturing a 2-minute YT recipe video produces a doc with 3-4 embedded keyframe images + grounded summary referencing both spoken and visual content.
- [ ] Backwards compatible — `video_analysis_enabled=False` makes the feature inert; no behavior change vs Phase 12 captures.
- [ ] Cost-bounded — average video <$0.01 in vision API spend; 8 frames × ~$0.001 each + the final summary call.
- [ ] Failures don't break the pipeline — if scenedetect / vision / blob upload throws, the doc still gets the audio-only body and `extra.video_analysis_failed: true` for ops visibility.
- [ ] No degradation in capture latency for short videos — scene detection on a 60s reel completes in <3s on the ingest container's CPU.
- [ ] Cookies (Phase 12) work transitively — if YouTube cookies are present, video download succeeds for previously-blocked content.
