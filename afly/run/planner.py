"""Turn selected extracts into a concrete `afly run` plan: one ChunkJob per
(extract, app, date-chunk), grouped into waves ClickHouse can rebuild from.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from afly.config.discovery import LoadedExtract
from afly.config.project_config import QuotaConfig
from afly.run.options import RunOptions
from afly.run.windows import Chunk, chunk_window, compute_window, is_long_call


@dataclass(frozen=True)
class _FallbackChunkLoad:
    """Field-compatible stand-in for ``afly.database.loads.ChunkLoad``.

    Used only if that module can't be imported yet (this milestone is being
    developed concurrently with the ClickHouse layer) — see
    :meth:`ChunkJob.load`. Every field name/order matches the real
    dataclass, so any consumer that reads it by attribute (a real or fake
    ``LoadsRepo`` alike) can't tell the difference.
    """

    run_id: str
    extract: str
    app_id: str
    report_type: str
    from_date: date
    to_date: date
    chunk_days: int
    is_long: bool


def _chunk_load_cls() -> type[Any]:
    try:
        from afly.database.loads import ChunkLoad

        return ChunkLoad
    except ImportError:
        return _FallbackChunkLoad


@dataclass
class ChunkJob:
    """One AppsFlyer pull + idempotent write: one extract, one app, one chunk."""

    extract: LoadedExtract
    app_id: str
    chunk: Chunk
    is_long: bool
    db: str
    table: str

    @property
    def pair(self) -> tuple[str, str]:
        """The ``(extract, app_id)`` identity used for coverage/idempotency."""
        return self.extract.config.name, self.app_id

    @property
    def key(self) -> tuple[str, str]:
        """The ``(app_id, report_type)`` AppsFlyer rate-limit scope this job draws from."""
        return self.app_id, self.extract.config.report_type

    def load(self, run_id: str) -> Any:
        """Build the ``ChunkLoad`` this job corresponds to for the loads ledger.

        Imports ``afly.database.loads.ChunkLoad`` lazily — that module
        belongs to a different, concurrently-developed milestone — and
        falls back to a field-compatible local stand-in if it isn't
        importable yet, so planner/executor tests never have to wait on the
        ClickHouse layer landing first.
        """
        return _chunk_load_cls()(
            run_id=run_id,
            extract=self.extract.config.name,
            app_id=self.app_id,
            report_type=self.extract.config.report_type,
            from_date=self.chunk.from_date,
            to_date=self.chunk.to_date,
            chunk_days=self.chunk.days,
            is_long=self.is_long,
        )


@dataclass
class ExtractPlan:
    """One extract's slice of the plan: which apps, what window, which jobs."""

    extract: LoadedExtract
    apps: list[str]
    window: tuple[date, date] | None
    jobs: list[ChunkJob]
    note: str | None = None


@dataclass
class Plan:
    """The whole `afly run` plan: every extract's slice, plus the flat job list."""

    extracts: list[ExtractPlan]
    jobs: list[ChunkJob]

    def waves(self) -> list[list[ChunkJob]]:
        """Jobs grouped by ``chunk.index``, ascending — the execution order.

        A wave groups every job whose chunk covers the same epoch-aligned
        slice, across every extract — so a day's ClickHouse partition is
        rebuilt exactly once, after every extract's contribution to it for
        this run has been fetched, not once per extract.
        """
        by_index: dict[int, list[ChunkJob]] = {}
        for job in self.jobs:
            by_index.setdefault(job.chunk.index, []).append(job)
        return [by_index[i] for i in sorted(by_index)]

    def tables(self) -> set[tuple[str, str]]:
        """Every distinct ``(db, table)`` destination this plan writes to."""
        return {(job.db, job.table) for job in self.jobs}

    def long_jobs(self) -> list[ChunkJob]:
        """Every job that draws against AppsFlyer's daily long-call budget."""
        return [job for job in self.jobs if job.is_long]


def build_plan(
    selected: list[LoadedExtract],
    *,
    apps_for: Callable[[LoadedExtract], list[str]],
    watermark_for: Callable[[str, str], date | None],
    today: date,
    default_db: str,
    quota: QuotaConfig,
    options: RunOptions,
) -> Plan:
    """Resolve *selected* extracts into a full :class:`Plan`.

    ``quota`` supplies ``long_call_min_days`` (needed to classify each chunk
    as long/short) — it isn't itself part of ``RunOptions`` since it's
    project config, not a CLI knob for this run.
    """
    extract_plans: list[ExtractPlan] = []
    all_jobs: list[ChunkJob] = []

    for extract in selected:
        config = extract.config
        apps = apps_for(extract)
        if options.apps is not None:
            wanted = set(options.apps)
            apps = [a for a in apps if a in wanted]

        if not apps:
            extract_plans.append(
                ExtractPlan(
                    extract=extract,
                    apps=[],
                    window=None,
                    jobs=[],
                    note="nothing to do (no apps selected)",
                )
            )
            continue

        db, table = config.resolved_table(default_db)
        # LoadedExtract.config has already gone through ExtractConfig.with_defaults()
        # (afly.config.discovery.load_extracts), which fills every one of these from
        # the project defaults (and hard-fails if start_date is still unset) — so
        # they're guaranteed non-None here even though the model type is Optional.
        assert config.start_date is not None
        assert config.lookback_days is not None
        assert config.include_current_day is not None
        assert config.chunk_days is not None
        chunk_days = options.chunk_days if options.chunk_days is not None else config.chunk_days

        jobs: list[ChunkJob] = []
        windows: list[tuple[date, date]] = []

        for app_id in apps:
            watermark = watermark_for(config.name, app_id)
            window = compute_window(
                today=today,
                start_date=config.start_date,
                lookback_days=config.lookback_days,
                include_current_day=config.include_current_day,
                watermark=watermark,
                from_override=options.from_date,
                to_override=options.to_date,
                full_refresh=options.full_refresh,
            )
            if window is None:
                continue
            windows.append(window)
            start, end = window
            for chunk in chunk_window(start, end, chunk_days):
                jobs.append(
                    ChunkJob(
                        extract=extract,
                        app_id=app_id,
                        chunk=chunk,
                        is_long=is_long_call(chunk, quota.long_call_min_days),
                        db=db,
                        table=table,
                    )
                )

        if not jobs:
            note: str | None = "nothing to do (window empty)"
            extract_window = None
        else:
            note = None
            extract_window = (min(w[0] for w in windows), max(w[1] for w in windows))

        extract_plans.append(
            ExtractPlan(extract=extract, apps=apps, window=extract_window, jobs=jobs, note=note)
        )
        all_jobs.extend(jobs)

    return Plan(extracts=extract_plans, jobs=all_jobs)


# render_plan lives in afly.run._render (split out to keep this module under
# the house line-length norm) — re-exported here so `from afly.run.planner
# import render_plan` keeps working for every existing call site.
from afly.run._render import render_plan  # noqa: E402

__all__ = ["ChunkJob", "ExtractPlan", "Plan", "build_plan", "render_plan"]
