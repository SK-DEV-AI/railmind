"""Request-log and metrics tests.

The point of these is that a number quoted in a report must be reproducible from
the file, and that an empty or damaged file cannot produce a confident number.
"""

from __future__ import annotations

import datetime as dt

import pytest

from railmind.engine.metrics import (
    T90_BUDGET_MS,
    RequestLog,
    RequestRecord,
    percentile,
)


def record(request_id: str, elapsed_ms: float, *, at: dt.datetime | None = None, **over):
    fields = {
        "request_id": request_id,
        "at": at or dt.datetime.now(dt.UTC),
        "route": "search",
        "elapsed_ms": elapsed_ms,
        "stage_ms": {"intent": 1.0, "search": elapsed_ms - 1.0},
        "result_count": 3,
        "degraded": False,
    }
    fields.update(over)
    return RequestRecord(**fields)


def test_percentile_matches_the_textbook_definition():
    values = [10.0, 20.0, 30.0, 40.0]

    assert percentile(values, 0.0) == 10.0
    assert percentile(values, 0.5) == 25.0
    assert percentile(values, 1.0) == 40.0
    assert percentile([7.0], 0.9) == 7.0


def test_percentile_refuses_nonsense():
    with pytest.raises(ValueError, match="empty sample"):
        percentile([], 0.5)
    with pytest.raises(ValueError, match="between 0 and 1"):
        percentile([1.0], 1.5)


def test_records_round_trip_through_the_file(tmp_path):
    log = RequestLog(tmp_path / "requests.jsonl")
    original = record("req-1", 742.5)

    log.append(original)
    read_back = log.records()

    assert len(read_back) == 1
    assert read_back[0].request_id == "req-1"
    assert read_back[0].elapsed_ms == pytest.approx(742.5)
    assert read_back[0].stage_ms["search"] == pytest.approx(741.5)


def test_a_truncated_final_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "requests.jsonl"
    log = RequestLog(path)
    log.append(record("req-1", 100.0))
    log.append(record("req-2", 200.0))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"request_id": "req-3", "at": "2026-10-25T1')  # killed mid-write

    records = log.records()

    assert [item.request_id for item in records] == ["req-1", "req-2"]


def test_missing_log_file_reports_no_traffic_instead_of_failing(tmp_path):
    log = RequestLog(tmp_path / "never-written.jsonl")

    assert log.records() == []
    metrics = log.metrics()
    assert metrics.sample_size == 0
    assert metrics.t90_ms is None
    assert metrics.stages.intent.n == 0
    assert "request_log" == metrics.counted_from


def test_t90_comes_from_the_log_and_the_budget_is_named(tmp_path):
    log = RequestLog(tmp_path / "requests.jsonl")
    for index in range(10):
        log.append(record(f"req-{index}", 100.0 * (index + 1)))

    metrics = log.metrics()

    assert metrics.sample_size == 10
    # 90th percentile of 100..1000 by linear interpolation.
    assert metrics.t90_ms == pytest.approx(910.0)
    assert metrics.stages.search.n == 10
    assert metrics.requests.errors == 0
    assert metrics.requests.degraded == 0
    assert T90_BUDGET_MS == 2000.0


def test_zero_recorded_calls_is_not_a_perfect_cache_hit_rate(tmp_path):
    log = RequestLog(tmp_path / "requests.jsonl")
    log.append(record("req-1", 120.0))

    metrics = log.metrics()

    assert metrics.provider_calls is None
    assert metrics.cache_hits is None
    assert metrics.cache_hit_rate is None


def test_reported_counters_are_summed_and_the_rate_follows(tmp_path):
    log = RequestLog(tmp_path / "requests.jsonl")
    log.append(record("req-1", 120.0, upstream_calls=2, cache_hits=6))
    log.append(record("req-2", 130.0, upstream_calls=1, cache_hits=1))

    metrics = log.metrics()

    assert metrics.provider_calls == 3
    assert metrics.cache_hits == 7
    assert metrics.cache_hit_rate == pytest.approx(0.7, abs=1e-4)


def test_errors_and_degraded_are_counted_separately(tmp_path):
    log = RequestLog(tmp_path / "requests.jsonl")
    log.append(record("req-1", 120.0, degraded=True))
    log.append(record("req-2", 130.0, error="invalid_query"))

    metrics = log.metrics()

    assert metrics.requests.total == 2
    assert metrics.requests.errors == 1
    assert metrics.requests.degraded == 1


def test_the_window_is_respected_without_sleeping(tmp_path):
    log = RequestLog(tmp_path / "requests.jsonl")
    now = dt.datetime(2026, 10, 25, 12, 0, tzinfo=dt.UTC)
    log.append(record("old", 100.0, at=now - dt.timedelta(hours=3)))
    log.append(record("new", 200.0, at=now - dt.timedelta(minutes=5)))

    recent = log.metrics(window_seconds=3600, now=now)

    assert recent.sample_size == 1
    assert recent.t90_ms == pytest.approx(200.0)


def test_only_search_requests_define_t90(tmp_path):
    log = RequestLog(tmp_path / "requests.jsonl")
    log.append(record("search-1", 500.0))
    log.append(record("handoff-1", 5000.0, route="handoff", stage_ms={"handoff": 5000.0}))

    metrics = log.metrics()

    # A slow human-facing handoff must not inflate the engine's headline number.
    assert metrics.t90_ms == pytest.approx(500.0)
    assert metrics.stages.handoff.n == 1
    assert metrics.stages.handoff.max_ms == pytest.approx(5000.0)
