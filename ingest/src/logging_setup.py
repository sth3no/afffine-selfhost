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


# Loggers we silence to WARNING. They're either too chatty or low-signal:
#   - uvicorn.access: a GET /health line every 5s drowns out everything else
#   - httpcore: low-level connection-pool plumbing
#   - asyncpg.pool: connection acquire/release events
_SILENCED_LOGGERS = (
    "uvicorn.access",
    "httpcore",
    "asyncpg.pool",
)


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
    """Configure root logger to emit JSON lines on stdout.

    Idempotent. The first call wins; subsequent calls bail. This guards
    against accidental re-entry (e.g., uvicorn re-importing the app).

    Aggressively strips handlers from EVERY existing logger and forces
    propagation to root, so the only emit path is root's JsonFormatter
    handler. Without this sweep, frameworks that pre-attach their own
    StreamHandler (uvicorn especially) keep emitting through their
    native format AND through our JSON, and at high concurrency the
    bytes interleave on stdout — producing the mangled
    `INFO INFO INFO ts=... logger=...` output that's been making
    production logs unreadable.

    Also silences a small set of chatty loggers (uvicorn.access for
    health-check spam, httpcore, asyncpg.pool) to WARNING.

    Emits a one-shot diagnostic log line at the end so operators can
    verify in `docker logs` that THIS function ran (vs. an old image
    still serving). Look for `setup_logging_complete` — it MUST appear
    as a single clean JSON line. If you see the mangled
    `INFO INFO INFO ...` pattern around that message, the deployed
    image is older than commit 50285b4 (May 9 2026) and the container
    needs a `docker compose up --build`.
    """
    root = logging.getLogger()

    # Idempotency: if our JsonFormatter is already on root, bail.
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root.setLevel(level)
    root.handlers[:] = [handler]

    # Walk every existing logger, strip its handlers, force propagation
    # to root. Snapshot the keys first because we mutate the registry.
    stripped_count = 0
    for name in list(logging.Logger.manager.loggerDict.keys()):
        existing = logging.Logger.manager.loggerDict.get(name)
        if not isinstance(existing, logging.Logger):
            # Skip PlaceHolder entries (lazy parent slots).
            continue
        if existing.handlers:
            stripped_count += 1
        existing.handlers[:] = []
        existing.propagate = True

    # Quiet the noise — these still go through root JSON, just at a higher
    # threshold so the signal lines aren't drowned out.
    for name in _SILENCED_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    # Diagnostic. Operators read this in `docker logs` to confirm the
    # current image actually has the JSON-clean logging fix deployed.
    logging.getLogger(__name__).info(
        "setup_logging_complete",
        extra={
            "formatter": type(handler.formatter).__name__,
            "root_handlers": len(root.handlers),
            "stripped_logger_handlers": stripped_count,
        },
    )


def audit_log_handlers() -> dict[str, object]:
    """Snapshot of the current logging-handler topology.

    Returns counts + per-logger handler info. Useful for a /diagnostic
    endpoint or ad-hoc check when the operator suspects the JSON
    formatter isn't actually active. If `extra_handler_loggers` is
    non-empty, some logger has had an EMITTING handler re-attached
    AFTER setup_logging ran — which would re-introduce the byte-
    interleaving that produces mangled `INFO INFO INFO ts=...` output.

    NullHandler attachments are deliberately ignored. The Python
    library convention is `logging.getLogger(__name__).addHandler(NullHandler())`
    to suppress "no handlers could be found" warnings when a library is
    used without its host configuring logging. NullHandler.emit() is
    a no-op — it can't produce duplicate output. Flagging these would
    make the audit perpetually noisy: any time `requests`, `urllib3`,
    `charset_normalizer`, etc. get imported (which our pipeline does
    transitively via youtube-transcript-api), they'd appear here even
    though they pose zero risk to log integrity.

    Cheap (~microseconds) — safe to call on every healthcheck if useful.
    """
    root = logging.getLogger()
    extra_handler_loggers: list[str] = []
    for name in list(logging.Logger.manager.loggerDict.keys()):
        existing = logging.Logger.manager.loggerDict.get(name)
        if not isinstance(existing, logging.Logger):
            continue
        emitting_handlers = [
            h for h in existing.handlers if not isinstance(h, logging.NullHandler)
        ]
        if emitting_handlers:
            extra_handler_loggers.append(name)
    return {
        "root_handler_count": len(root.handlers),
        "root_formatter": type(root.handlers[0].formatter).__name__ if root.handlers else None,
        "extra_handler_loggers": extra_handler_loggers,
        "json_formatter_active": any(
            isinstance(h.formatter, JsonFormatter) for h in root.handlers
        ),
    }


@contextlib.contextmanager
def set_capture_id(capture_id: str):
    """Bind capture_id to the current async/sync context."""
    token = capture_id_var.set(capture_id)
    try:
        yield
    finally:
        capture_id_var.reset(token)
