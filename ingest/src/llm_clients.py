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


def anthropic_client() -> AsyncAnthropic:
    global _anthropic
    if _anthropic is None:
        _anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _anthropic


def openai_client() -> AsyncOpenAI:
    global _openai
    if _openai is None:
        _openai = AsyncOpenAI(api_key=settings.openai_api_key)
    return _openai
