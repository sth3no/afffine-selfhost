"""Per-capture LLM/API usage accounting.

The worker installs a `UsageCollector` into a contextvar for the duration
of one `process_capture` run (mirroring `logging_setup.set_capture_id`);
every LLM call site records its response usage into it via the
`record_*` helpers below. When the capture finishes — success OR failure —
the worker persists the aggregated summary to `captures.cost_breakdown`
(JSONB) and emits one structured log line, so spend per platform/topic is
greppable and queryable.

Paths that run without a collector (API-triggered /rerender, tests that
don't install one) make the `record_*` helpers no-ops — call sites never
need to care whether accounting is active.

Token counts only, no dollar math: prices change, `usage` doesn't.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_collector_var: ContextVar["UsageCollector | None"] = ContextVar(
    "llm_usage_collector", default=None,
)

# Aggregation key: (kind, model). Everything else is summed.
_COUNTER_FIELDS = (
    "calls",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "bytes_in",
)


class UsageCollector:
    """Accumulates usage events for one capture. Not thread-safe by design —
    one capture runs on one event loop, and child tasks (chunked render's
    asyncio.gather) share this object through the copied context."""

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []

    def record(
        self,
        *,
        kind: str,
        model: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        bytes_in: int = 0,
    ) -> None:
        self._events.append({
            "kind": kind,
            "model": model,
            "calls": 1,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "bytes_in": bytes_in,
        })

    def summary(self) -> dict[str, Any] | None:
        """Aggregate events by (kind, model). None when nothing was recorded
        (caller skips the DB write + log line entirely)."""
        if not self._events:
            return None
        grouped: dict[tuple[str, str | None], dict[str, Any]] = {}
        for e in self._events:
            key = (e["kind"], e["model"])
            bucket = grouped.setdefault(
                key,
                {"kind": e["kind"], "model": e["model"],
                 **{f: 0 for f in _COUNTER_FIELDS}},
            )
            for f in _COUNTER_FIELDS:
                bucket[f] += e[f]
        totals = {f: sum(b[f] for b in grouped.values()) for f in _COUNTER_FIELDS}
        return {"calls": list(grouped.values()), "totals": totals}


@contextmanager
def collect_usage():
    """Install a fresh collector for the current context. Yields it."""
    collector = UsageCollector()
    token = _collector_var.set(collector)
    try:
        yield collector
    finally:
        _collector_var.reset(token)


def _as_int(value: Any) -> int:
    """Coerce SDK usage attributes defensively — mocked responses in tests
    carry MagicMock attributes, and None appears for absent counters."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def record_anthropic_usage(response: Any, *, kind: str, model: str) -> None:
    """Record `response.usage` from an Anthropic messages call. No-op when
    no collector is active or the response carries no usage."""
    collector = _collector_var.get()
    if collector is None:
        return
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    collector.record(
        kind=kind,
        model=model,
        input_tokens=_as_int(getattr(usage, "input_tokens", 0)),
        output_tokens=_as_int(getattr(usage, "output_tokens", 0)),
        cache_read_tokens=_as_int(getattr(usage, "cache_read_input_tokens", 0)),
        cache_write_tokens=_as_int(getattr(usage, "cache_creation_input_tokens", 0)),
    )


def record_openai_embedding_usage(response: Any, *, model: str) -> None:
    """Record an OpenAI embeddings response (usage.total_tokens)."""
    collector = _collector_var.get()
    if collector is None:
        return
    usage = getattr(response, "usage", None)
    collector.record(
        kind="embedding",
        model=model,
        input_tokens=_as_int(getattr(usage, "total_tokens", 0)) if usage else 0,
    )


def record_whisper_usage(*, bytes_in: int, model: str = "whisper-1") -> None:
    """Record a Whisper transcription. The API bills by audio minute, which
    we don't reliably know — upload bytes are the stable proxy we do."""
    collector = _collector_var.get()
    if collector is None:
        return
    collector.record(kind="whisper", model=model, bytes_in=_as_int(bytes_in))
