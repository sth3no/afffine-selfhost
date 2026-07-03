"""Process-wide LLM SDK clients.

Every pipeline module used to construct a fresh AsyncAnthropic/AsyncOpenAI
per call, which defeats HTTP connection pooling and scatters retry/timeout
configuration across seven call sites. These lazy singletons centralize it.

Tests patch `anthropic_client` / `openai_client` at the importing module
(e.g. `patch("src.pipeline.classifier.anthropic_client")`) — the call-site
shape `client = anthropic_client()` is mock-friendly by design.
"""

from __future__ import annotations

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from src.config import settings

_anthropic: AsyncAnthropic | None = None
_openai: AsyncOpenAI | None = None


# Explicit budgets instead of implicit SDK defaults, sized to the heaviest
# real calls. Anthropic: a 4096-token chunked-render reduce or a 12-image
# vision call finishes well under 5 minutes. OpenAI: a near-25 MB Whisper
# upload + transcription can take several minutes on a slow uplink, so it
# gets the longer budget. Both sit comfortably inside the worker's
# CAPTURE_TIMEOUT_SEC (default 1800 s) even with retries.
ANTHROPIC_TIMEOUT_SEC = 300.0
OPENAI_TIMEOUT_SEC = 600.0
MAX_RETRIES = 2


def anthropic_client() -> AsyncAnthropic:
    global _anthropic
    if _anthropic is None:
        _anthropic = AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            timeout=ANTHROPIC_TIMEOUT_SEC,
            max_retries=MAX_RETRIES,
        )
    return _anthropic


def openai_client() -> AsyncOpenAI:
    global _openai
    if _openai is None:
        _openai = AsyncOpenAI(
            api_key=settings.openai_api_key,
            timeout=OPENAI_TIMEOUT_SEC,
            max_retries=MAX_RETRIES,
        )
    return _openai
