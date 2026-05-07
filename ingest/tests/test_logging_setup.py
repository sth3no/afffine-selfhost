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


def test_setup_logging_attaches_json_formatter_to_root(monkeypatch, caplog):
    """After setup_logging(), the root handler's formatter is JsonFormatter."""
    setup_logging(level="INFO")
    root = logging.getLogger()
    assert any(isinstance(h.formatter, JsonFormatter) for h in root.handlers)


def test_extra_fields_included_in_payload():
    record = logging.LogRecord("test", logging.INFO, "x", 1, "msg", (), None)
    record.platform = "instagram"
    record.duration_ms = 123
    payload = _format_one(record)
    assert payload["platform"] == "instagram"
    assert payload["duration_ms"] == 123
