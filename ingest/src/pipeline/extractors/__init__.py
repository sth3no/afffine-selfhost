"""Extractor registry.

Each extractor is an async function with signature
    async def extract(url: str, platform: Platform, **kwargs) -> Extracted

The orchestrator may pass additional kwargs (`mcp_client`, `capture_id`,
forward-compat additions) — extractors should swallow unknown ones via
`**_kwargs`. This lets us add Phase 13 video analysis to cobalt_ext
without changing every other extractor's signature.

The mapping from extractor name (string in topics.yaml's `extractor:` field)
to the function lives in `_REGISTRY`. Built-ins are registered at import
time by side effect of `from . import markitdown_ext, ytdlp_ext, ...`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.config import Platform
    from src.pipeline.extracted import Extracted


# Relaxed signature: positional (url, platform) + arbitrary kwargs.
ExtractFunc = Callable[..., Awaitable["Extracted"]]


_REGISTRY: dict[str, ExtractFunc] = {}


def register_extractor(name: str, fn: ExtractFunc) -> None:
    _REGISTRY[name] = fn


def get_extractor(name: str) -> ExtractFunc:
    if name not in _REGISTRY:
        raise KeyError(f"no extractor named {name!r}; registered: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


# Side-effect imports register the built-ins.
from src.pipeline.extractors import (  # noqa: E402, F401
    markitdown_ext,
    ytdlp_ext,
    oembed_ytdlp_ext,
    reddit_json_ext,
    cobalt_ext,
)
