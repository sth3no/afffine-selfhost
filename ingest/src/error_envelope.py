"""Contract-shaped error envelope + FastAPI handlers.

Contract (matches what the iOS client expects):

    { "error": { "code": "...", "message": "...", "trace_id": "..." } }

iOS recognises INVALID_TOKEN, RATE_LIMITED, and INTERNAL as special-cased
codes. Anything else surfaces its `message` to the user. Without this
module FastAPI returns its default `{"detail": "..."}` shape, which the
iOS client treats as "no envelope" and shows "Server error, retry." even
for benign 4xx like missing-field validation.

The trace_id is a ULID set per-request by `trace_id_middleware` (api.py)
into `logging_setup.trace_id_var` so every log line emitted during the
request shares it. The same id is echoed in the response body and the
`X-Trace-Id` response header — paste any of those into a server-log grep
to find the matching stack trace.
"""

from __future__ import annotations

import logging
import traceback

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from ulid import ULID

from src.logging_setup import trace_id_var

log = logging.getLogger(__name__)


_STATUS_TO_CODE = {
    400: "VALIDATION_FAILED",
    401: "INVALID_TOKEN",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "VALIDATION_FAILED",
    429: "RATE_LIMITED",
    503: "SERVICE_UNAVAILABLE",
}


def _trace_id() -> str:
    return trace_id_var.get(None) or str(ULID())


def _envelope(
    code: str,
    message: str,
    *,
    trace_id: str,
    status_code: int,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "trace_id": trace_id}},
        headers={"X-Trace-Id": trace_id},
    )


async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    trace_id = _trace_id()
    code = _STATUS_TO_CODE.get(
        exc.status_code,
        "BAD_REQUEST" if 400 <= exc.status_code < 500 else "INTERNAL",
    )
    message = str(exc.detail) if exc.detail is not None else "request failed"
    log.warning(
        "http_exception",
        extra={
            "status_code": exc.status_code,
            "code": code,
            "detail": message,
        },
    )
    return _envelope(code, message, trace_id=trace_id, status_code=exc.status_code)


async def validation_exception_handler(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    trace_id = _trace_id()
    errors = exc.errors()
    if errors:
        first = errors[0]
        loc_parts = [str(p) for p in first.get("loc", []) if p != "body"]
        loc = ".".join(loc_parts)
        msg_text = str(first.get("msg", "invalid"))
        message = f"{loc}: {msg_text}" if loc else msg_text
    else:
        message = "validation failed"
    log.warning("validation_failed", extra={"errors": errors})
    return _envelope(
        "VALIDATION_FAILED",
        message,
        trace_id=trace_id,
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    trace_id = _trace_id()
    log.error(
        "unhandled_exception",
        extra={
            "method": request.method,
            "path": request.url.path,
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
            "traceback": "".join(traceback.format_exception(exc)),
        },
    )
    # Surface the exception type + a short slice of its message in the body
    # so the iOS Diagnostics dump is immediately useful when pasted back —
    # the full traceback stays in server logs only. Truncated to keep the
    # 5xx body small (iOS truncates >4KB).
    short = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else ""
    message = f"{type(exc).__name__}: {short}" if short else type(exc).__name__
    return _envelope(
        "INTERNAL",
        message,
        trace_id=trace_id,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def register(app: FastAPI) -> None:
    """Register all envelope handlers. Call once at app construction."""
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
