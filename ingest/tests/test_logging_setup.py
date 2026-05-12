import io
import json
import logging

import pytest

from src.logging_setup import (
    JsonFormatter,
    capture_id_var,
    set_capture_id,
    setup_logging,
)


def _format_one(record: logging.LogRecord) -> dict:
    return json.loads(JsonFormatter().format(record))


def test_json_formatter_emits_required_fields():
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="x.py", lineno=1,
        msg="hello", args=(), exc_info=None,
    )
    payload = _format_one(record)
    assert payload["msg"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test"
    assert "ts" in payload


def test_json_formatter_includes_capture_id_when_set():
    token = capture_id_var.set("01J-X")
    try:
        record = logging.LogRecord("test", logging.INFO, "x", 1, "msg", (), None)
        payload = _format_one(record)
        assert payload["capture_id"] == "01J-X"
    finally:
        capture_id_var.reset(token)


def test_json_formatter_omits_capture_id_when_unset():
    record = logging.LogRecord("test", logging.INFO, "x", 1, "msg", (), None)
    payload = _format_one(record)
    assert "capture_id" not in payload


def test_set_capture_id_context_manager_resets():
    """The token is removed when the with block exits."""
    assert capture_id_var.get(None) is None
    with set_capture_id("01J-A"):
        assert capture_id_var.get(None) == "01J-A"
    assert capture_id_var.get(None) is None


@pytest.fixture
def _reset_logging():
    """Reset root + named loggers between tests so global logging state
    from one test doesn't leak into another."""
    root = logging.getLogger()
    saved_root_handlers = list(root.handlers)
    saved_root_level = root.level
    yield
    root.handlers[:] = saved_root_handlers
    root.setLevel(saved_root_level)


def test_setup_logging_attaches_json_formatter_to_root(_reset_logging):
    """After setup_logging(), the root handler's formatter is JsonFormatter."""
    root = logging.getLogger()
    root.handlers[:] = []
    setup_logging(level="INFO")
    assert any(isinstance(h.formatter, JsonFormatter) for h in root.handlers)


def test_setup_logging_is_idempotent(_reset_logging):
    """Second call doesn't add another handler — guards against double-emit
    when setup_logging is accidentally re-entered."""
    root = logging.getLogger()
    root.handlers[:] = []
    setup_logging(level="INFO")
    first = len(root.handlers)
    setup_logging(level="INFO")
    assert len(root.handlers) == first


def test_setup_logging_strips_pre_attached_logger_handlers(_reset_logging):
    """If uvicorn (or any framework) pre-attached a handler to a sublogger,
    setup_logging clears it so output goes through root JSON only."""
    root = logging.getLogger()
    root.handlers[:] = []
    fake_uvicorn = logging.getLogger("uvicorn")
    fake_uvicorn.handlers[:] = [logging.StreamHandler()]
    fake_uvicorn.propagate = False
    setup_logging(level="INFO")
    assert fake_uvicorn.handlers == []
    assert fake_uvicorn.propagate is True


def test_setup_logging_silences_chatty_loggers(_reset_logging):
    """uvicorn.access health-check spam, httpcore, asyncpg.pool drop to WARNING."""
    root = logging.getLogger()
    root.handlers[:] = []
    setup_logging(level="INFO")
    for name in ("uvicorn.access", "httpcore", "asyncpg.pool"):
        assert logging.getLogger(name).level == logging.WARNING, name


def test_extra_fields_included_in_payload():
    record = logging.LogRecord("test", logging.INFO, "x", 1, "msg", (), None)
    record.platform = "instagram"
    record.duration_ms = 123
    payload = _format_one(record)
    assert payload["platform"] == "instagram"
    assert payload["duration_ms"] == 123


def test_audit_log_handlers_reports_clean_topology(_reset_logging):
    """After a fresh setup, the audit reports json_formatter_active=True
    and no per-logger handlers. This is the green-state diagnostic."""
    from src.logging_setup import audit_log_handlers
    root = logging.getLogger()
    root.handlers[:] = []
    setup_logging(level="INFO")
    audit = audit_log_handlers()
    assert audit["json_formatter_active"] is True
    assert audit["root_handler_count"] == 1
    assert audit["root_formatter"] == "JsonFormatter"
    assert audit["extra_handler_loggers"] == []


def test_audit_log_handlers_flags_re_attached_handlers(_reset_logging):
    """Defensive: if a framework re-attaches a handler to a sublogger
    AFTER setup_logging ran, the audit reports it. The operator sees
    `extra_handler_loggers=['suspect.framework']` in the diagnostic
    endpoint and knows the byte-interleaving symptom is back."""
    from src.logging_setup import audit_log_handlers
    root = logging.getLogger()
    root.handlers[:] = []
    setup_logging(level="INFO")
    # Simulate a framework re-attaching its own handler post-setup.
    suspect = logging.getLogger("suspect.framework")
    suspect.handlers[:] = [logging.StreamHandler()]
    suspect.propagate = False
    audit = audit_log_handlers()
    assert "suspect.framework" in audit["extra_handler_loggers"]


def test_setup_logging_emits_diagnostic_marker(_reset_logging, capsys):
    """The setup_logging_complete log line must appear after handler
    install — operators read it in `docker logs` to confirm the new
    image is actually running. Marker must be in JSON form (not the
    mangled fallback) which proves JsonFormatter is on the emit path."""
    root = logging.getLogger()
    root.handlers[:] = []
    setup_logging(level="INFO")
    captured = capsys.readouterr()
    # The marker line must be a parseable JSON line, not mangled output.
    assert "setup_logging_complete" in captured.out
    # Find the line and make sure it parses as JSON with the right fields.
    marker_lines = [
        line for line in captured.out.splitlines()
        if "setup_logging_complete" in line
    ]
    assert marker_lines, f"no marker line in output: {captured.out!r}"
    payload = json.loads(marker_lines[0])
    assert payload["msg"] == "setup_logging_complete"
    assert payload["formatter"] == "JsonFormatter"
    assert payload["root_handlers"] == 1
