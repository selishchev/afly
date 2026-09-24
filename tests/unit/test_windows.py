"""Unit tests for afly.run.windows — pure date-window and chunking math."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from afly.run.windows import Chunk, chunk_window, compute_window, is_long_call

_DAY = timedelta(days=1)
_TODAY = date(2026, 9, 22)
_START = date(2026, 1, 1)


def _window(**overrides: object) -> tuple[date, date] | None:
    kwargs: dict[str, object] = {
        "today": _TODAY,
        "start_date": _START,
        "lookback_days": 3,
        "include_current_day": True,
        "watermark": None,
        "from_override": None,
        "to_override": None,
        "full_refresh": False,
    }
    kwargs.update(overrides)
    return compute_window(**kwargs)  # type: ignore[arg-type]


# -- compute_window -----------------------------------------------------


@pytest.mark.unit
def test_steady_state_watermark_is_today() -> None:
    # watermark == today: start = min(today - 3, today - 2) = today - 3
    result = _window(watermark=_TODAY)
    assert result == (_TODAY - _DAY * 3, _TODAY)


@pytest.mark.unit
def test_ten_day_gap_reopens_back_to_watermark() -> None:
    # watermark 10 days stale: start = min(today-3, watermark-2) = watermark-2
    watermark = _TODAY - _DAY * 10
    result = _window(watermark=watermark)
    assert result is not None
    assert result[0] == watermark - _DAY * 2
    assert result[1] == _TODAY


@pytest.mark.unit
def test_empty_table_no_watermark_uses_start_date() -> None:
    result = _window(watermark=None)
    assert result == (_START, _TODAY)


@pytest.mark.unit
def test_floor_by_start_date() -> None:
    # watermark is only 1 day old, but lookback would reach before start_date.
    watermark = _START + _DAY * 1
    result = _window(watermark=watermark, start_date=_START, lookback_days=3)
    assert result is not None
    assert result[0] == _START  # floored, not watermark - 2


@pytest.mark.unit
def test_include_current_day_false_excludes_today() -> None:
    result = _window(watermark=_TODAY, include_current_day=False)
    assert result is not None
    assert result[1] == _TODAY - _DAY


@pytest.mark.unit
def test_from_override_wins_over_watermark_logic() -> None:
    override = date(2026, 8, 1)
    result = _window(watermark=_TODAY, from_override=override)
    assert result is not None
    assert result[0] == override


@pytest.mark.unit
def test_to_override_clamps_end() -> None:
    override = _TODAY - _DAY * 5
    # watermark=None keeps start pinned at start_date, well before the
    # override, so only the "end" clamp is under test here.
    result = _window(watermark=None, to_override=override)
    assert result is not None
    assert result[1] == override


@pytest.mark.unit
def test_full_refresh_ignores_watermark_uses_start_date() -> None:
    result = _window(watermark=_TODAY, full_refresh=True)
    assert result == (_START, _TODAY)


@pytest.mark.unit
def test_lookback_seven_days() -> None:
    result = _window(watermark=_TODAY, lookback_days=7)
    assert result is not None
    assert result[0] == _TODAY - _DAY * 7


@pytest.mark.unit
def test_inverted_window_returns_none() -> None:
    # to_override before start_date -> nothing to do.
    result = _window(to_override=_START - _DAY * 1)
    assert result is None


@pytest.mark.unit
def test_from_override_after_to_override_returns_none() -> None:
    result = _window(
        from_override=_TODAY,
        to_override=_TODAY - _DAY * 1,
    )
    assert result is None


# -- Chunk ----------------------------------------------------------------


@pytest.mark.unit
def test_chunk_days_inclusive() -> None:
    chunk = Chunk(index=0, from_date=date(2026, 1, 1), to_date=date(2026, 1, 2))
    assert chunk.days == 2


@pytest.mark.unit
def test_chunk_dates_inclusive_list() -> None:
    chunk = Chunk(index=0, from_date=date(2026, 1, 1), to_date=date(2026, 1, 3))
    assert chunk.dates() == [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]


@pytest.mark.unit
def test_single_day_chunk_has_one_day() -> None:
    chunk = Chunk(index=0, from_date=date(2026, 1, 1), to_date=date(2026, 1, 1))
    assert chunk.days == 1


# -- chunk_window -----------------------------------------------------------


@pytest.mark.unit
def test_chunk_window_empty_range_returns_empty_list() -> None:
    assert chunk_window(date(2026, 1, 5), date(2026, 1, 1), 2) == []


@pytest.mark.unit
def test_chunk_window_rejects_non_positive_chunk_days() -> None:
    with pytest.raises(ValueError):
        chunk_window(date(2026, 1, 1), date(2026, 1, 5), 0)


@pytest.mark.unit
def test_chunk_window_epoch_alignment_shared_across_extracts() -> None:
    """Two extracts with different windows but the same chunk_days agree on boundaries."""
    chunk_days = 3
    window_a = (date(2026, 1, 1), date(2026, 1, 10))
    window_b = (date(2026, 1, 4), date(2026, 1, 7))

    chunks_a = chunk_window(*window_a, chunk_days)
    chunks_b = chunk_window(*window_b, chunk_days)

    # Every chunk index that appears in both must have identical epoch
    # boundaries before window-clipping — verify by looking up the same
    # index in both series and checking the underlying epoch math agrees.
    indices_a = {c.index for c in chunks_a}
    indices_b = {c.index for c in chunks_b}
    shared = indices_a & indices_b
    assert shared, "expected overlapping chunk indices between the two windows"

    for index in shared:
        # The epoch boundary for a given index is a pure function of
        # (index, chunk_days) alone — recompute it and check both series'
        # chunks fall inside it.
        epoch_start = date.fromordinal(index * chunk_days)
        epoch_end = date.fromordinal((index + 1) * chunk_days - 1)
        for chunks in (chunks_a, chunks_b):
            chunk = next(c for c in chunks if c.index == index)
            assert epoch_start <= chunk.from_date <= chunk.to_date <= epoch_end


@pytest.mark.unit
def test_chunk_window_every_day_belongs_to_exactly_one_chunk() -> None:
    start, end = date(2026, 1, 1), date(2026, 1, 20)
    chunks = chunk_window(start, end, 3)
    all_days: list[date] = []
    for chunk in chunks:
        all_days.extend(chunk.dates())
    expected = [start + _DAY * i for i in range((end - start).days + 1)]
    assert all_days == expected


@pytest.mark.unit
def test_chunk_window_edge_clipping() -> None:
    """A window starting/ending mid-epoch is clipped to the requested range, not the epoch."""
    chunk_days = 4
    # 2026-01-01 is day-of-epoch index 0 boundary-dependent; pick a window
    # that starts and ends mid-chunk to exercise both clip sides.
    start = date(2026, 1, 3)
    end = date(2026, 1, 9)
    chunks = chunk_window(start, end, chunk_days)
    assert chunks[0].from_date == start
    assert chunks[-1].to_date == end
    # interior boundaries (if any) must be untouched epoch boundaries.
    for chunk in chunks[1:-1]:
        assert chunk.days == chunk_days


@pytest.mark.unit
def test_chunk_window_single_day_range() -> None:
    chunks = chunk_window(date(2026, 5, 1), date(2026, 5, 1), 2)
    assert len(chunks) == 1
    assert chunks[0].from_date == chunks[0].to_date == date(2026, 5, 1)


# -- is_long_call -------------------------------------------------------


@pytest.mark.unit
def test_is_long_call_true_when_at_or_above_threshold() -> None:
    chunk = Chunk(index=0, from_date=date(2026, 1, 1), to_date=date(2026, 1, 3))
    assert chunk.days == 3
    assert is_long_call(chunk, long_call_min_days=3) is True


@pytest.mark.unit
def test_is_long_call_false_when_below_threshold() -> None:
    chunk = Chunk(index=0, from_date=date(2026, 1, 1), to_date=date(2026, 1, 2))
    assert chunk.days == 2
    assert is_long_call(chunk, long_call_min_days=3) is False
