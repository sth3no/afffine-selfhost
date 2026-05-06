from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.models import (
    CaptureRequest,
    CaptureResponse,
    CaptureStatus,
    normalized_url,
    url_hash,
)


def test_capture_request_minimal():
    req = CaptureRequest(url="https://example.com/page")
    assert req.url == "https://example.com/page"
    assert req.source_app is None
    assert req.shared_title is None
    assert req.shared_text is None


def test_capture_request_full():
    req = CaptureRequest(
        url="https://www.instagram.com/p/Cxyz/",
        source_app="Instagram",
        shared_title="Honey-glazed salmon",
        shared_text="Recipe with photos",
    )
    assert req.source_app == "Instagram"
    assert req.shared_title == "Honey-glazed salmon"


def test_capture_request_at_least_url_or_text():
    """Spec §4 says one of url/shared_text must be present. With neither,
    Pydantic accepts (since both are Optional), but the API handler
    enforces the rule. Test the model accepts; handler test covers the rule."""
    req = CaptureRequest(url=None, shared_text="just a note")
    assert req.url is None
    assert req.shared_text == "just a note"


def test_capture_request_rejects_non_http_scheme():
    with pytest.raises(ValidationError):
        CaptureRequest(url="javascript:alert(1)")


def test_capture_response_serializes_iso8601():
    resp = CaptureResponse(
        capture_id="01J9X4M5",
        doc_id="aaaa-bbbb",
        web_url="https://affine.example.com/workspace/x/aaaa-bbbb",
        status=CaptureStatus.QUEUED,
        platform="instagram",
        initial_path="Sources/Socials/Instagram",
        created_at=datetime(2026, 5, 7, 14, 20, 0, tzinfo=timezone.utc),
    )
    payload = resp.model_dump(mode="json")
    assert payload["status"] == "queued"
    assert payload["created_at"] == "2026-05-07T14:20:00Z"


def test_normalized_url_strips_utm_params_and_lowercases_host():
    nu = normalized_url("https://Instagram.COM/p/abc?utm_source=test&id=1#section")
    # host lowercased, utm_* stripped, fragment dropped, other params kept
    assert nu == "https://instagram.com/p/abc?id=1"


def test_normalized_url_strips_trailing_slash_when_no_query():
    nu = normalized_url("https://example.com/foo/")
    assert nu == "https://example.com/foo"


def test_normalized_url_keeps_trailing_slash_when_root():
    nu = normalized_url("https://example.com/")
    assert nu == "https://example.com/"


def test_url_hash_is_stable_across_normalization_inputs():
    a = url_hash("https://Instagram.COM/p/abc?utm_source=x")
    b = url_hash("https://instagram.com/p/abc")
    assert a == b
    assert len(a) == 64  # sha256 hex
