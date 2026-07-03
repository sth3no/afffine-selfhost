"""Tests for per-capture LLM usage accounting (src/llm_usage.py) and its
worker integration (cost_breakdown persistence)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.llm_usage import (
    UsageCollector,
    collect_usage,
    record_anthropic_usage,
    record_openai_embedding_usage,
    record_whisper_usage,
)


def _anthropic_response(*, input_tokens=100, output_tokens=20,
                        cache_read=0, cache_write=0):
    return SimpleNamespace(usage=SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    ))


# ── Collector aggregation ─────────────────────────────────────────────


def test_summary_is_none_when_nothing_recorded():
    assert UsageCollector().summary() is None


def test_summary_aggregates_by_kind_and_model():
    c = UsageCollector()
    c.record(kind="render_map", model="sonnet", input_tokens=100, output_tokens=10)
    c.record(kind="render_map", model="sonnet", input_tokens=200, output_tokens=20)
    c.record(kind="classify", model="haiku", input_tokens=50, output_tokens=5)

    summary = c.summary()
    by_key = {(e["kind"], e["model"]): e for e in summary["calls"]}
    assert by_key[("render_map", "sonnet")]["calls"] == 2
    assert by_key[("render_map", "sonnet")]["input_tokens"] == 300
    assert by_key[("classify", "haiku")]["output_tokens"] == 5
    assert summary["totals"]["calls"] == 3
    assert summary["totals"]["input_tokens"] == 350
    assert summary["totals"]["output_tokens"] == 35


# ── record_* helpers ─────────────────────────────────────────────────


def test_record_helpers_noop_without_collector():
    # Must not raise, must not require a collector.
    record_anthropic_usage(_anthropic_response(), kind="classify", model="haiku")
    record_openai_embedding_usage(SimpleNamespace(usage=None), model="emb")
    record_whisper_usage(bytes_in=123)


def test_record_anthropic_usage_reads_usage_fields():
    with collect_usage() as c:
        record_anthropic_usage(
            _anthropic_response(input_tokens=11, output_tokens=7,
                                cache_read=3, cache_write=2),
            kind="render", model="sonnet",
        )
    s = c.summary()
    assert s["totals"] == {
        "calls": 1, "input_tokens": 11, "output_tokens": 7,
        "cache_read_tokens": 3, "cache_write_tokens": 2, "bytes_in": 0,
    }


def test_record_anthropic_usage_tolerates_mocked_responses():
    """Test suites mock parse() responses with MagicMock — usage attributes
    then aren't real ints. Accounting must coerce and never raise (MagicMock
    coincidentally coerces to 1; non-coercible values become 0)."""
    with collect_usage() as c:
        record_anthropic_usage(MagicMock(), kind="classify", model="haiku")
        record_anthropic_usage(
            SimpleNamespace(usage=SimpleNamespace(
                input_tokens="garbage", output_tokens=None,
                cache_read_input_tokens=object(),
                cache_creation_input_tokens=[],
            )),
            kind="classify", model="haiku",
        )
    s = c.summary()
    assert s["totals"]["calls"] == 2
    # The garbage-valued event contributed zeros; MagicMock contributed 1s.
    assert s["totals"]["input_tokens"] <= 2


def test_record_whisper_usage_records_bytes():
    with collect_usage() as c:
        record_whisper_usage(bytes_in=4096)
    assert c.summary()["totals"]["bytes_in"] == 4096


def test_collector_is_scoped_to_the_context():
    with collect_usage() as c:
        record_whisper_usage(bytes_in=1)
    # Outside the block the var is reset — this must be a silent no-op.
    record_whisper_usage(bytes_in=999)
    assert c.summary()["totals"]["bytes_in"] == 1


# ── Call-site wiring (classifier as the representative site) ─────────


@pytest.mark.asyncio
async def test_classifier_records_usage_into_active_collector():
    from src.config import Platform
    from src.pipeline.classification import ClassificationResult
    from src.pipeline.classifier import classify
    from src.pipeline.extracted import Extracted, MediaKind

    response = _anthropic_response(input_tokens=123, output_tokens=45)
    response.parsed_output = ClassificationResult(
        topic="Tech", confidence=0.9, reasoning="r",
    )
    client = MagicMock()
    client.messages.parse = AsyncMock(return_value=response)

    with patch("src.pipeline.classifier.anthropic_client", return_value=client):
        with collect_usage() as c:
            await classify(
                extracted=Extracted(
                    title="t", body_md="b", author=None, published_at=None,
                    media_kind=MediaKind.TEXT, extra={},
                ),
                platform=Platform(id="article", group="Articles",
                                  folder_name="Web", hosts=["*"],
                                  extractor="markitdown"),
                sibling_topics=[],
                topic_hints=[],
            )

    s = c.summary()
    assert s["calls"][0]["kind"] == "classify"
    assert s["totals"]["input_tokens"] == 123
    assert s["totals"]["output_tokens"] == 45


# ── Worker persistence ────────────────────────────────────────────────


def _make_pool():
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


def _worker_row():
    from datetime import datetime, timezone
    from src.db import CaptureRow
    return CaptureRow(
        id="01J-u", url="https://example.com", url_hash="h",
        source_app=None, shared_title=None, shared_text=None,
        platform="article", status="extracting", doc_id="d", web_url="w",
        topic_path="Sources/Articles/Web",
        created_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
    )


async def _run_one_worker_iteration(process_fn):
    from src.config import Platform, TopicsConfig
    from src.worker import Worker

    plat = Platform(id="article", group="Articles", folder_name="Web",
                    hosts=["*"], extractor="markitdown")
    repo = AsyncMock()
    rows = [_worker_row()]

    async def _claim(*a, **k):
        return rows.pop(0) if rows else None

    repo.claim_next_queued.side_effect = _claim
    repo.claim_due_failed.return_value = None

    w = Worker(
        pool=_make_pool(),
        repo_factory=lambda conn: repo,
        process_fn=process_fn,
        platform_for=lambda row: plat,
        topics=TopicsConfig(platforms=[plat]),
        poll_interval_sec=0.01,
    )
    import asyncio
    task = asyncio.create_task(w._loop())
    await asyncio.sleep(0.05)
    w.stop()
    await asyncio.wait_for(task, timeout=2.0)
    return repo


@pytest.mark.asyncio
async def test_worker_persists_cost_breakdown_on_success():
    async def _process(row, **kwargs):
        record_whisper_usage(bytes_in=2048)

    repo = await _run_one_worker_iteration(_process)
    repo.save_cost_breakdown.assert_awaited_once()
    kwargs = repo.save_cost_breakdown.await_args.kwargs
    assert kwargs["capture_id"] == "01J-u"
    assert kwargs["breakdown"]["totals"]["bytes_in"] == 2048


@pytest.mark.asyncio
async def test_worker_persists_cost_breakdown_on_failure():
    """Spend incurred before a failure is still accounted — the failed
    attempt paid for those calls."""
    async def _process(row, **kwargs):
        record_whisper_usage(bytes_in=512)
        raise RuntimeError("boom after billable work")

    repo = await _run_one_worker_iteration(_process)
    repo.mark_failed.assert_awaited_once()
    repo.save_cost_breakdown.assert_awaited_once()
    assert repo.save_cost_breakdown.await_args.kwargs["breakdown"]["totals"]["bytes_in"] == 512


@pytest.mark.asyncio
async def test_worker_skips_persistence_when_nothing_recorded():
    repo = await _run_one_worker_iteration(AsyncMock())
    repo.save_cost_breakdown.assert_not_called()
