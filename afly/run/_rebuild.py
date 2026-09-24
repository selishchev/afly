"""Idempotent per-wave rebuild: the empty-response guard + PartitionRebuilder calls.

Split out of ``executor.py`` as a free function so the rebuild pass (which
touches every table/day this wave fetched) can be reasoned about without the
surrounding wave/job-dispatch bookkeeping.

Grouping by partition (not by day) is the point of this module post
configurable-granularity: a wave's touched days are bucketed by
``partition_id(day, rebuilder.granularity)`` first, and ``rebuild_partition``
is called once per partition — under the default ``month`` granularity a
2-day chunk that straddles a calendar-month boundary produces *two*
partition rebuilds in the same wave, not one. Each partition's ``coverage``
and ``fresh_rows`` are built *only* from this wave's own days — a later wave
that rebuilds the same (still month-wide) partition again passes only its
own days, never this wave's, and vice versa (see
``PartitionRebuilder.rebuild_partition``'s docstring for why that keeps a
month partition safe to rebuild across several consecutive waves).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

from afly.database.ddl import partition_id as compute_partition_id
from afly.run._job_result import JobResult
from afly.run.options import RunOptions
from afly.run.planner import ChunkJob


def rebuild_wave(
    wave_jobs: list[ChunkJob],
    wave_results: dict[int, JobResult],
    buffers: Mapping[tuple[str, str, date], list[dict[str, Any]]],
    rebuilders: Mapping[tuple[str, str], Any],
    options: RunOptions,
    days_rebuilt: list[dict[str, Any]],
    echo_guard: Callable[[ChunkJob, str], None],
) -> None:
    """Rebuild every ClickHouse partition this wave touched.

    Mutates *wave_results* in place: a job whose fetch returned 0 rows,
    whose extract has ``on_empty == "skip"``, and ``not options.allow_empty``
    gets downgraded from ``"success"`` to ``"skipped"`` — the empty-response
    guard — the moment any of its covered days turns out to already hold
    data for that ``(extract, app)`` pair, protecting real history from a
    transient empty AppsFlyer response. A pair with no existing rows on any
    of its days is left as a genuine (and unprotected) ``"success"`` with
    ``rows == 0``. Appends one entry to *days_rebuilt* per ``(table,
    partition)`` actually rebuilt, carrying a ``"days"`` field (the specific
    days of *this wave* that fed that partition) so the JSON summary stays
    human-readable even though the rebuild key is now the partition, not the
    day.
    """
    tables = sorted({(j.db, j.table) for j in wave_jobs})
    guard_hits: dict[int, int] = {}

    for db, table in tables:
        rebuilder = rebuilders[(db, table)]
        jobs_for_table = [j for j in wave_jobs if (j.db, j.table) == (db, table)]
        succeeded = [
            j
            for j in jobs_for_table
            if wave_results.get(id(j)) and wave_results[id(j)].status == "success"
        ]

        days: set[date] = set()
        for j in succeeded:
            days.update(j.chunk.dates())

        days_by_partition: dict[str, list[date]] = {}
        for day in sorted(days):
            pid = compute_partition_id(day, rebuilder.granularity)
            days_by_partition.setdefault(pid, []).append(day)

        for pid in sorted(days_by_partition):
            partition_days = days_by_partition[pid]
            coverage: set[tuple[date, str, str]] = set()
            for day in partition_days:
                coverage |= _coverage_for_day(
                    day, succeeded, wave_results, options, rebuilder, guard_hits
                )
            fresh_rows: list[dict[str, Any]] = []
            for day in partition_days:
                fresh_rows.extend(buffers.get((db, table, day), []))

            res = rebuilder.rebuild_partition(pid, coverage, fresh_rows)
            days_rebuilt.append(
                {
                    "table": f"{db}.{table}",
                    "partition": pid,
                    "days": partition_days,
                    "action": res.action,
                    "kept_rows": res.kept_rows,
                    "fresh_rows": res.fresh_rows,
                }
            )

    _apply_guard_downgrades(guard_hits, wave_results, echo_guard)


def _coverage_for_day(
    day: date,
    succeeded: list[ChunkJob],
    wave_results: dict[int, JobResult],
    options: RunOptions,
    rebuilder: Any,
    guard_hits: dict[int, int],
) -> set[tuple[date, str, str]]:
    coverage: set[tuple[date, str, str]] = set()
    for j in succeeded:
        if not (j.chunk.from_date <= day <= j.chunk.to_date):
            continue
        result = wave_results[id(j)]
        if result.rows == 0 and j.extract.config.on_empty == "skip" and not options.allow_empty:
            existing = rebuilder.rows_for_pair(day, j.extract.config.name, j.app_id)
            if existing > 0:
                guard_hits[id(j)] = guard_hits.get(id(j), 0) + existing
                continue
        coverage.add((day, *j.pair))
    return coverage


def _apply_guard_downgrades(
    guard_hits: dict[int, int],
    wave_results: dict[int, JobResult],
    echo_guard: Callable[[ChunkJob, str], None],
) -> None:
    for job_id, existing_rows in guard_hits.items():
        result = wave_results.get(job_id)
        if result is None:
            continue
        result.status = "skipped"
        result.skip_reason = f"empty response guard: {existing_rows} existing rows kept"
        echo_guard(result.job, result.skip_reason)


__all__ = ["rebuild_wave"]
