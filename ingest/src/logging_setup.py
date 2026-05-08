"""Structured JSON logging for the ingest service.

One JSON line per log record on stdout. Portainer / docker logs aggregate
these; pipe through `jq` for ad-hoc inspection.

A contextvars-backed `capture_id` token is auto-included in every record
emitted while inside a `set_capture_id(...)` block — the worker wraps
each capture's pipeline in this so all related log lines share the
correlation key.

Usage:
    setup_logging()                       # at startup
    with set_capture_id("01J-X"):         # in the worker
        await process_capture(row, ...)
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


capture_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "capture_id", default=None,
)

trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trace_id", default=None,
)


# Standard LogRecord attributes — anything else is treated as user-supplied
# `extra=` and emitted into the JSON payload.
_STANDARD_RECORD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "asctime", "message", "taskName",
})


class JsonFormatter(logging.Formatter):
    """Render LogRecord as a single JSON line with capture_id correlation."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        cap = capture_id_var.get(None)
        if cap:
            payload["capture_id"] = cap
        tid = trace_id_var.get(None)
        if tid:
            payload["trace_id"] = tid

        # Attach exception info if present.
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        # Pick up user-supplied extras (anything not in the standard attrs).
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRS or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)

        return json.dumps(payload, ensure_ascii=False)


def setup_logging(*, level: str | int = "INFO") -> None:
    """Configure root logger to emit JSON lines on stdout. Idempotent."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.setLevel(level)
    # Drop any existing handlers (e.g., uvicorn's default) so we don't
    # double-log.
    root.handlers[:] = [handler]
    root.propagate = False


@contextlib.contextmanager
def set_capture_id(capture_id: str):
    """Bind capture_id to the current async/sync context."""
    token = capture_id_var.set(capture_id)
    try:
        yield
    finally:
        capture_id_var.reset(token)
