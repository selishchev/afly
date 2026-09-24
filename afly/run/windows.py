"""Pure date-window math for `afly run`: which days to pull, and how to chunk them.

Nothing in this module talks to AppsFlyer, ClickHouse, or the filesystem —
that's deliberate, so the tricky date arithmetic (watermark gaps, epoch-
aligned chunk boundaries) can be exhaustively unit-tested without any fakes
at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from afly.utils.datetime_utils import daterange


def compute_window(
    *,
    today: date,
    start_date: date,
    lookback_days: int,
    include_current_day: bool,
    watermark: date | None,
    from_override: date | None,
    to_override: date | None,
    full_refresh: bool,
) -> tuple[date, date] | None:
    """The inclusive ``(from, to)`` window to pull for one extract/app pair.

    ``end`` is today (or yesterday, if the extract excludes the current day),
    clamped down by ``--to`` when given.

    ``start`` is, in priority order: ``--from`` if given; else ``start_date``
    if this is a full refresh or nothing has ever loaded successfully; else
    the earlier of ``today - lookback_days`` (the extract's normal rolling
    window) and ``watermark - lookback_days + 1`` (re-opens the last
    ``lookback_days`` worth of days *before* the watermark too — those are
    still "settling", e.g. late-arriving attribution — and, if the watermark
    is older than that because a run was down for a while, pulls the whole
    gap back to it automatically rather than leaving a silent hole).

    ``start`` is always floored at ``start_date`` — the earliest day the
    extract is configured to care about. Returns ``None`` when the resulting
    window is empty/inverted (``start > end``): "nothing to do", not an
    error.
    """
    end = today if include_current_day else today - timedelta(days=1)
    if to_override is not None:
        end = min(end, to_override)

    if from_override is not None:
        start = from_override
    elif full_refresh or watermark is None:
        start = start_date
    else:
        start = min(
            today - timedelta(days=lookback_days),
            watermark - timedelta(days=lookback_days - 1),
        )

    start = max(start, start_date)

    if start > end:
        return None
    return start, end


@dataclass(frozen=True)
class Chunk:
    """One epoch-aligned slice of a pull window — one AppsFlyer request."""

    index: int
    from_date: date
    to_date: date

    @property
    def days(self) -> int:
        """Inclusive day count spanned by this chunk."""
        return (self.to_date - self.from_date).days + 1

    def dates(self) -> list[date]:
        """Every date in this chunk, inclusive, in order."""
        return daterange(self.from_date, self.to_date)


def chunk_window(start: date, end: date, chunk_days: int) -> list[Chunk]:
    """Split ``[start, end]`` into epoch-aligned chunks of (up to) *chunk_days*.

    "Epoch-aligned" means the chunk boundaries are anchored to
    ``date.toordinal() // chunk_days`` rather than to *start* — so two
    extracts sharing the same ``chunk_days`` (the common case, since it
    usually comes from the project default) always agree on where a chunk
    starts and ends, and every calendar day belongs to exactly one chunk
    index regardless of which window happens to include it. That matters for
    :class:`~afly.run.planner.Plan.waves`: jobs are grouped by ``chunk.index``
    into waves so afly rebuilds each day's partition only once it has every
    extract's contribution to it, not once per extract.

    Returns an empty list for an inverted/empty range (``start > end``).
    """
    if chunk_days < 1:
        raise ValueError(f"chunk_days must be >= 1, got {chunk_days}")
    if start > end:
        return []

    start_index = start.toordinal() // chunk_days
    end_index = end.toordinal() // chunk_days

    chunks: list[Chunk] = []
    for index in range(start_index, end_index + 1):
        epoch_start = date.fromordinal(index * chunk_days)
        epoch_end = date.fromordinal((index + 1) * chunk_days - 1)
        chunks.append(
            Chunk(
                index=index,
                from_date=max(start, epoch_start),
                to_date=min(end, epoch_end),
            )
        )
    return chunks


def is_long_call(chunk: Chunk, long_call_min_days: int) -> bool:
    """Whether *chunk* falls into AppsFlyer's daily-budget ("long call") tier."""
    return chunk.days >= long_call_min_days
