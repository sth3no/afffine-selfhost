from datetime import datetime, timezone

from src.models import CaptureDetail, CaptureItem, CapturesPage, CaptureStatus


def _now():
    return datetime(2026, 5, 7, 14, 20, 0, tzinfo=timezone.utc)


def test_capture_item_serializes_iso8601_z():
    item = CaptureItem(
        capture_id="01J",
        url="https://x",
        platform="instagram",
        status=CaptureStatus.DONE,
        doc_id="d",
        web_url="w",
        topic_path="Sources/Socials/Instagram/Recipes",
        created_at=_now(),
        completed_at=_now(),
    )
    payload = item.model_dump(mode="json")
    assert payload["status"] == "done"
    assert payload["created_at"].endswith("Z")


def test_capture_item_optional_fields_default_to_none():
    item = CaptureItem(
        capture_id="01J", url=None, platform="article",
        status=CaptureStatus.QUEUED,
        doc_id=None, web_url=None, topic_path=None,
        created_at=_now(),
    )
    assert item.completed_at is None


def test_captures_page_with_items_and_cursor():
    item = CaptureItem(
        capture_id="01J", url=None, platform="article",
        status=CaptureStatus.QUEUED, doc_id=None, web_url=None,
        topic_path=None, created_at=_now(),
    )
    page = CapturesPage(items=[item], next_cursor=None)
    assert len(page.items) == 1
    assert page.next_cursor is None


def test_capture_detail_extends_item_with_diagnostics():
    detail = CaptureDetail(
        capture_id="01J", url=None, platform="article",
        status=CaptureStatus.FAILED,
        doc_id=None, web_url=None, topic_path=None,
        created_at=_now(),
        error="extractor failed",
        retry_count=2,
        classifier_reasoning=None,
    )
    assert detail.error == "extractor failed"
    assert detail.retry_count == 2
