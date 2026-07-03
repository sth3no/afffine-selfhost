"""End-to-end pin tests for the cost-guard paths.

These overlap with the Phase 4 extractor unit tests but cover the contract
explicitly and survive future refactors of the extractor internals.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import Platform, settings
from src.pipeline.extracted import truncate_body


def test_truncate_body_caps_at_max_body_chars():
    body = "x" * (settings.max_body_chars * 2)
    out = truncate_body(body, limit=settings.max_body_chars)
    assert len(out) <= settings.max_body_chars + 80  # marker is short
    assert out.endswith("[...truncated]")


def test_max_body_chars_default_is_50_000():
    assert settings.max_body_chars == 50_000


def test_max_transcript_min_default_is_30():
    assert settings.max_transcript_min == 30


@pytest.mark.asyncio
async def test_ytdlp_extractor_honors_max_transcript_min():
    """A 90-min YouTube without captions must NOT call Whisper API."""
    from src.pipeline.extractors.ytdlp_ext import extract

    info = {
        "id": "long-id",
        "title": "Long Video",
        "channel": "Ch",
        "duration": 90 * 60,  # 90 minutes — over the 30min cap
        "upload_date": "20260507",
        "description": "long video",
        "subtitles": {},
        "automatic_captions": {},
    }

    plat = Platform(id="youtube", group="Socials", folder_name="Youtube",
                    hosts=["youtube.com"], extractor="ytdlp")

    with patch("src.pipeline.extractors.ytdlp_ext._run_ytdlp_metadata", new_callable=AsyncMock) as run, \
         patch("src.pipeline.extractors.ytdlp_ext._run_ytdlp_audio", new_callable=AsyncMock) as audio, \
         patch("src.pipeline.extractors.ytdlp_ext._whisper_transcribe", new_callable=AsyncMock) as whisper:
        # Fake workdir with just the info.json (no caption files)
        import tempfile
        workdir = Path(tempfile.mkdtemp())
        (workdir / "video.info.json").write_text(json.dumps(info), encoding="utf-8")
        run.return_value = workdir

        result = await extract("https://www.youtube.com/watch?v=long", plat)

        # Cost guard: no audio extraction, no whisper call.
        audio.assert_not_called()
        whisper.assert_not_called()
        assert "transcript skipped" in result.body_md.lower()


@pytest.mark.asyncio
async def test_cobalt_extractor_honors_max_transcript_min():
    """A 90-min captionless video on the cobalt path must NOT call Whisper.

    The cobalt path is what youtube/instagram/tiktok/x actually route
    through per topics.yaml — the cap can't live only in the legacy ytdlp
    extractor."""
    import httpx
    from src.pipeline.extractors import cobalt_ext

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"status": "tunnel", "url": "http://cobalt:9000/tunnel/x"},
            )
        return httpx.Response(200, content=b"\x00" * 8192)

    plat = Platform(id="instagram", group="Socials", folder_name="Instagram",
                    hosts=["instagram.com"], extractor="cobalt")

    with patch.object(cobalt_ext, "_TEST_TRANSPORT", httpx.MockTransport(_handler)), \
         patch.object(cobalt_ext, "fetch_metadata",
                      AsyncMock(return_value={"title": "T", "duration": 90 * 60})), \
         patch.object(cobalt_ext, "_whisper_transcribe", new_callable=AsyncMock) as whisper:
        result = await cobalt_ext.extract("https://www.instagram.com/reel/x/", plat)

    whisper.assert_not_called()
    assert "transcript skipped" in result.body_md.lower()


@pytest.mark.asyncio
async def test_whisper_transcribe_refuses_oversize_upload(tmp_path, monkeypatch):
    """_whisper_transcribe must refuse files over the API's 25 MB upload
    cap with a descriptive error instead of letting OpenAI 413 it."""
    from src.pipeline.extractors import ytdlp_ext

    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"\x00" * 128)
    monkeypatch.setattr(ytdlp_ext, "_WHISPER_MAX_UPLOAD_BYTES", 64)

    with pytest.raises(RuntimeError, match="upload limit"):
        await ytdlp_ext._whisper_transcribe(audio)


@pytest.mark.asyncio
async def test_ytdlp_extractor_uses_caption_when_present_no_whisper():
    """Even for a short video, presence of caption skips Whisper (cost saver)."""
    from src.pipeline.extractors.ytdlp_ext import extract

    info = {
        "id": "short-id",
        "title": "Short Video",
        "channel": "Ch",
        "duration": 600,  # 10 min, under cap
        "upload_date": "20260507",
        "description": "short video",
    }

    plat = Platform(id="youtube", group="Socials", folder_name="Youtube",
                    hosts=["youtube.com"], extractor="ytdlp")

    with patch("src.pipeline.extractors.ytdlp_ext._run_ytdlp_metadata", new_callable=AsyncMock) as run, \
         patch("src.pipeline.extractors.ytdlp_ext._run_ytdlp_audio", new_callable=AsyncMock) as audio, \
         patch("src.pipeline.extractors.ytdlp_ext._whisper_transcribe", new_callable=AsyncMock) as whisper:
        import tempfile
        workdir = Path(tempfile.mkdtemp())
        (workdir / "video.info.json").write_text(json.dumps(info), encoding="utf-8")
        (workdir / "video.en.vtt").write_text(
            "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello\n", encoding="utf-8",
        )
        run.return_value = workdir

        result = await extract("https://www.youtube.com/watch?v=short", plat)

        audio.assert_not_called()
        whisper.assert_not_called()
        assert "hello" in result.body_md
