"""Unit tests for afly.run._rebuild.rebuild_wave — partition grouping.

Calls rebuild_wave directly (not through the full Executor, see
test_executor.py for that) so the day-to-partition grouping logic can be
asserted precisely: under the default `month` granularity, a wave's touched
days are bucketed by `partition_id(day, "month")` and `rebuild_partition` is
called once per partition, not once per day.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from afly.run._job_result import JobResult
from afly.run._rebuild import rebuild_wave
from afly.run.options import RunOptions
from afly.run.planner import ChunkJob
from afly.run.windows import Chunk

from .run_fakes import FakeRebuilder, make_loaded

_NOOP_GUARD = lambda job, reason: None  # noqa: E731


def _job(
    extract_name: str,
    app_id: str,
    from_date: date,
    to_date: date,
    *,
    db: str = "marts",
    table: str = "t",
    index: int = 0,
    on_empty: str = "skip",
) -> ChunkJob:
    extract = make_loaded(extract_name, table=table, on_empty=on_empty)
    chunk = Chunk(index=index, from_date=from_date, to_date=to_date)
    return ChunkJob(extract=extract, app_id=app_id, chunk=chunk, is_long=False, db=db, table=table)


def _run(
    jobs: list[ChunkJob],
    wave_results: dict[int, JobResult],
    buffers: dict[tuple[str, str, date], list[dict[str, Any]]],
    rebuilder: FakeRebuilder,
    *,
    options: RunOptions | None = None,
) -> list[dict[str, Any]]:
    days_rebuilt: list[dict[str, Any]] = []
    rebuild_wave(
        jobs,
        wave_results,
        buffers,
        {("marts", "t"): rebuilder},
        options or RunOptions(select="*"),
        days_rebuilt,
        _NOOP_GUARD,
    )
    return days_rebuilt


@pytest.mark.unit
def test_a_month_spanning_chunk_produces_two_partition_rebuilds() -> None:
    """A 2-day chunk that straddles a calendar-month boundary must become
    TWO separate rebuild_partition calls under the default month granularity
    — this is the exact scenario the spec calls out."""
    job = _job("standard", "app1", date(2026, 1, 31), date(2026, 2, 1))
    wave_results = {id(job): JobResult(job=job, status="success", rows=2)}
    buffers = {
        ("marts", "t", date(2026, 1, 31)): [{"date": date(2026, 1, 31), "app_id": "app1"}],
        ("marts", "t", date(2026, 2, 1)): [{"date": date(2026, 2, 1), "app_id": "app1"}],
    }
    rebuilder = FakeRebuilder(granularity="month")

    days_rebuilt = _run([job], wave_results, buffers, rebuilder)

    assert {c.partition_id for c in rebuilder.rebuild_calls} == {"202601", "202602"}
    jan_call = next(c for c in rebuilder.rebuild_calls if c.partition_id == "202601")
    feb_call = next(c for c in rebuilder.rebuild_calls if c.partition_id == "202602")
    assert jan_call.coverage == {(date(2026, 1, 31), "standard", "app1")}
    assert feb_call.coverage == {(date(2026, 2, 1), "standard", "app1")}
    assert jan_call.fresh_rows == [{"date": date(2026, 1, 31), "app_id": "app1"}]
    assert feb_call.fresh_rows == [{"date": date(2026, 2, 1), "app_id": "app1"}]

    assert {d["partition"] for d in days_rebuilt} == {"202601", "202602"}
    jan_entry = next(d for d in days_rebuilt if d["partition"] == "202601")
    assert jan_entry["table"] == "marts.t"
    assert jan_entry["days"] == [date(2026, 1, 31)]


@pytest.mark.unit
def test_a_same_month_chunk_stays_in_one_partition_rebuild() -> None:
    job = _job("standard", "app1", date(2026, 3, 5), date(2026, 3, 6))
    wave_results = {id(job): JobResult(job=job, status="success", rows=2)}
    buffers = {
        ("marts", "t", date(2026, 3, 5)): [{"date": date(2026, 3, 5), "app_id": "app1"}],
        ("marts", "t", date(2026, 3, 6)): [{"date": date(2026, 3, 6), "app_id": "app1"}],
    }
    rebuilder = FakeRebuilder(granularity="month")

    days_rebuilt = _run([job], wave_results, buffers, rebuilder)

    assert len(rebuilder.rebuild_calls) == 1
    call = rebuilder.rebuild_calls[0]
    assert call.partition_id == "202603"
    assert call.coverage == {
        (date(2026, 3, 5), "standard", "app1"),
        (date(2026, 3, 6), "standard", "app1"),
    }
    assert len(call.fresh_rows) == 2
    assert len(days_rebuilt) == 1
    assert days_rebuilt[0]["days"] == [date(2026, 3, 5), date(2026, 3, 6)]


@pytest.mark.unit
def test_day_granularity_still_produces_one_partition_per_day() -> None:
    job = _job("standard", "app1", date(2026, 3, 5), date(2026, 3, 6))
    wave_results = {id(job): JobResult(job=job, status="success", rows=2)}
    buffers = {
        ("marts", "t", date(2026, 3, 5)): [{"date": date(2026, 3, 5), "app_id": "app1"}],
        ("marts", "t", date(2026, 3, 6)): [{"date": date(2026, 3, 6), "app_id": "app1"}],
    }
    rebuilder = FakeRebuilder(granularity="day")

    _run([job], wave_results, buffers, rebuilder)

    assert {c.partition_id for c in rebuilder.rebuild_calls} == {"20260305", "20260306"}


@pytest.mark.unit
def test_successive_waves_rebuilding_the_same_month_partition_have_isolated_coverage() -> None:
    """Two consecutive waves both touch March 2026 (a different day each) —
    a real scenario when chunk_days is small relative to a month partition.
    Each wave's rebuild must carry only ITS OWN day's coverage/fresh_rows:
    an earlier wave's rebuild must never include a later wave's day (it
    hasn't been fetched yet), and a later wave's rebuild must never re-apply
    an earlier wave's day (that would re-derive it from whatever the
    now-current destination holds, not from stale buffered rows)."""
    rebuilder = FakeRebuilder(granularity="month")

    job1 = _job("standard", "app1", date(2026, 3, 1), date(2026, 3, 1), index=0)
    wave1_results = {id(job1): JobResult(job=job1, status="success", rows=1)}
    buffers1 = {("marts", "t", date(2026, 3, 1)): [{"date": date(2026, 3, 1), "app_id": "app1"}]}
    _run([job1], wave1_results, buffers1, rebuilder)

    job2 = _job("standard", "app1", date(2026, 3, 2), date(2026, 3, 2), index=1)
    wave2_results = {id(job2): JobResult(job=job2, status="success", rows=1)}
    buffers2 = {("marts", "t", date(2026, 3, 2)): [{"date": date(2026, 3, 2), "app_id": "app1"}]}
    _run([job2], wave2_results, buffers2, rebuilder)

    assert len(rebuilder.rebuild_calls) == 2
    call1, call2 = rebuilder.rebuild_calls
    assert call1.partition_id == call2.partition_id == "202603"
    assert call1.coverage == {(date(2026, 3, 1), "standard", "app1")}
    assert call2.coverage == {(date(2026, 3, 2), "standard", "app1")}
    # Neither call's fresh_rows leak the other wave's day.
    assert call1.fresh_rows == [{"date": date(2026, 3, 1), "app_id": "app1"}]
    assert call2.fresh_rows == [{"date": date(2026, 3, 2), "app_id": "app1"}]


@pytest.mark.unit
def test_empty_response_guard_applies_per_day_inside_a_grouped_partition() -> None:
    """A month-wide partition with two covered pairs — one day genuinely
    empty (no existing rows -> real success), the other day guarded (had
    existing rows, zero-row fetch is treated as a transient hiccup)."""
    job_day5 = _job(
        "standard", "app1", date(2026, 3, 5), date(2026, 3, 5), index=0, on_empty="skip"
    )
    job_day6 = _job(
        "standard", "app1", date(2026, 3, 6), date(2026, 3, 6), index=1, on_empty="skip"
    )
    wave_results = {
        id(job_day5): JobResult(job=job_day5, status="success", rows=0),
        id(job_day6): JobResult(job=job_day6, status="success", rows=0),
    }
    rebuilder = FakeRebuilder(
        granularity="month",
        existing_rows={(date(2026, 3, 6), "standard", "app1"): 7},  # day6 had data; day5 didn't
    )

    _run([job_day5, job_day6], wave_results, {}, rebuilder)

    assert wave_results[id(job_day5)].status == "success"  # nothing to guard
    assert wave_results[id(job_day6)].status == "skipped"
    assert wave_results[id(job_day6)].skip_reason == "empty response guard: 7 existing rows kept"
    # Only day5 (the ungarded pair) ends up in the rebuild's coverage.
    assert len(rebuilder.rebuild_calls) == 1
    assert rebuilder.rebuild_calls[0].coverage == {(date(2026, 3, 5), "standard", "app1")}
