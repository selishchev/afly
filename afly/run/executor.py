"""Executor — drives a Plan's waves: fetch, parse, buffer, rebuild, record.

A "wave" is every job sharing the same epoch-aligned chunk index (see
``windows.chunk_window``) across every extract in the plan. Rebuilds
(:func:`afly.run._rebuild.rebuild_wave`) always happen strictly in wave
order — a day's ClickHouse partition is rebuilt only once every extract's
contribution to it for this run has actually been fetched, and wave *N+1*
is never rebuilt before wave *N*.

*Dispatch* order is looser than that, on purpose: the executor admits up to
``quota.max_waves_in_flight`` waves into the scheduler at once (see
``_admit_more_waves``), so a rate-limited job in wave *N* can cool down
without idling every key that's ready in wave *N+1* — see
``afly.run.scheduler`` for why a single strictly-ordered wave used to stall
the whole run on one throttled key. Rows fetched for a not-yet-front wave sit
in ``_buffers`` (keyed by ``(db, table, day)``, same as before) until that
wave's own turn to rebuild; the pair invariant a later chunk must never be
applied if an earlier chunk of the same ``(extract, app)`` pair fails is
still enforced — see ``QuotaScheduler``'s docstring for how dispatch order
guarantees it, and ``Executor._process_job``'s ``_failed_pairs`` check for
where a later chunk actually gets skipped once that happens.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import click

from afly.appsflyer.client import AppsFlyerClient
from afly.appsflyer.retry import RetryPolicy
from afly.cli._output import echo_warning
from afly.run import _echo
from afly.run._fetch import AUTH_ABORT, DEFERRED, TRANSIENT_DEFERRED, FetchOutcome, process_job
from afly.run._job_result import JobResult, job_result_to_dict
from afly.run._protocols import LoadsRepoLike, RebuilderLike
from afly.run._rebuild import rebuild_wave
from afly.run._wave_window import WaveTracker
from afly.run.options import RunOptions
from afly.run.planner import ChunkJob, Plan
from afly.run.scheduler import QuotaScheduler
from afly.run.summary import RunSummary

_ABORT_REASONS = {
    "auth": "aborted: authentication failed",
    "clickhouse": "aborted: ClickHouse error",
}

_STAT_KEY = {"success": "succeeded", "failed": "failed", "skipped": "skipped"}


class Executor:
    def __init__(
        self,
        *,
        plan: Plan,
        scheduler: QuotaScheduler,
        client: AppsFlyerClient,
        policy_factory: Callable[[], RetryPolicy],
        loads: LoadsRepoLike,
        rebuilders: dict[tuple[str, str], RebuilderLike],
        run_id: str,
        now: Callable[[], datetime],
        options: RunOptions,
        summary: RunSummary,
        currency_for: Callable[[str], str | None] = lambda app_id: None,
        echo: Callable[[str], None] = click.echo,
    ) -> None:
        self.plan = plan
        self.scheduler = scheduler
        self.client = client
        self.policy_factory = policy_factory
        self.loads = loads
        self.rebuilders = rebuilders
        self.run_id = run_id
        self.now = now
        self.options = options
        self.summary = summary
        # Known app currency (afly.appsflyer.mng_api.AppInfo.currency, via
        # afly.run._apps.AppsResolver) — best-effort: returns None whenever
        # the account-wide app list was never fetched this run (every
        # selected extract named explicit `apps:`), in which case
        # ReportContext.currency falls back to per-report header detection.
        self.currency_for = currency_for
        self.echo = echo

        self._failed_pairs: set[tuple[str, str]] = set()
        self._wave_results: dict[int, JobResult] = {}
        self._buffers: dict[tuple[str, str, date], list[dict[str, Any]]] = {}
        self._skip_cursor = 0
        self._warned_headers: set[str] = set()
        self._extract_stats: dict[str, dict[str, int]] = {}
        # Per-job state that spans a rate-limit deferral: a job re-dispatched
        # after `QuotaScheduler.defer` is the *same* ChunkJob object (keyed
        # by id()), so `loads.start_chunk` fires once and `api_calls`
        # accumulates across every attempt instead of resetting.
        self._job_started_at: dict[int, datetime] = {}
        self._pending_api_calls: dict[int, int] = {}
        # Same idea as `_pending_api_calls`, for a job currently cooling down
        # from a deferred TransientError: the latest (http_status, error
        # detail) seen, kept in case the scheduler eventually gives up and
        # this has to become a terminal failed JobResult (see
        # `_process_job`'s TRANSIENT_DEFERRED handling).
        self._pending_transient: dict[int, tuple[int | None, str]] = {}

        # Wave-lookahead bookkeeping — set fresh at the top of `run()` (a
        # Plan's waves are static, known up front, so there's nothing to
        # gain by tracking them lazily). See `WaveTracker` for what it owns;
        # `_current_abort_reason` is the executor's own "should we even
        # admit/dispatch anything more" flag, kept separate since it's a
        # concern the tracker doesn't need to know about.
        self._wave_tracker: WaveTracker = WaveTracker([])
        self._current_abort_reason: str | None = None

    def run(self) -> None:
        self._wave_tracker = WaveTracker(self.plan.waves())
        self._current_abort_reason = None

        self._admit_more_waves()

        while self._wave_tracker.front < self._wave_tracker.total_waves:
            job = self.scheduler.next_job()
            if job is None:
                # Nothing dispatchable in the admitted window right now.
                # `_advance_front` is what keeps the window topped up as
                # waves complete, so try it before concluding there's
                # genuinely nothing left to do.
                front_before = self._wave_tracker.front
                self._advance_front()
                if self._wave_tracker.front == front_before:
                    # Front wave still isn't complete, yet the scheduler has
                    # nothing queued for it either — every admitted job
                    # should already be terminal or still in the scheduler's
                    # own queue (see afly.run.scheduler's module docstring),
                    # so reaching this is a bookkeeping bug, not a normal
                    # state. Stop instead of spinning forever.
                    break
                continue

            if self._current_abort_reason is not None:
                # A job the scheduler had already deferred (rate-limited,
                # re-queued for a later attempt) before the abort landed
                # still needs its accumulated api_calls/started_at — see the
                # DEFERRED handling in `_process_job`.
                jid = id(job)
                api_calls = self._pending_api_calls.pop(jid, 0)
                started_at = self._job_started_at.pop(jid, None)
                self._record_result(
                    job,
                    "skipped",
                    api_calls=api_calls,
                    skip_reason=self._current_abort_reason,
                    started_at=started_at,
                )
            else:
                signal = self._process_job(job)
                if signal == AUTH_ABORT:
                    self._current_abort_reason = _ABORT_REASONS["auth"]
                    self.summary.aborted = "auth"

            self._drain_scheduler_skips()
            self._advance_front()

        self._finalize_extract_stats()

    # -- wave window: admission + completion ------------------------------

    def _admit_more_waves(self) -> None:
        """Top the lookahead window back up to ``quota.max_waves_in_flight`` waves.

        Once aborted, a not-yet-admitted wave is never handed to the
        scheduler at all — its jobs are marked terminal directly, matching
        "every remaining job (this wave + later) is skipped" from the old
        strict-wave abort contract.
        """
        window = self.scheduler.quota.max_waves_in_flight
        while self._wave_tracker.has_room_to_admit(window):
            wave_jobs = self._wave_tracker.next_wave_to_admit()
            if self._current_abort_reason is not None:
                for job in wave_jobs:
                    self._record_result(job, "skipped", skip_reason=self._current_abort_reason)
            else:
                self.scheduler.load_wave(wave_jobs)

    def _advance_front(self) -> None:
        """Rebuild+finish every wave, strictly in wave order, that's now complete.

        A wave is complete once every one of its jobs is terminal (see
        ``WaveTracker.wave_complete``) — dispatch order across the lookahead
        window is deliberately *not* wave order (see the module docstring),
        so a later wave can go complete before an earlier one; this only
        ever acts on the tracker's ``front`` (the oldest not-yet-rebuilt
        wave), which is what keeps rebuilds themselves strictly ordered
        regardless of dispatch order. Tops the admission window back up
        after every advance so it stays ``max_waves_in_flight`` waves deep
        for as long as any remain.
        """
        tracker = self._wave_tracker
        while tracker.front < tracker.total_waves and tracker.wave_complete(tracker.front):
            self._complete_wave(tracker.waves[tracker.front])
            tracker.front += 1
            self._admit_more_waves()

    def _complete_wave(self, wave_jobs: list[ChunkJob]) -> None:
        # Deliberately not gated on `_current_abort_reason is None`: the wave
        # *during which* an abort lands can still hold real successes fetched
        # before the abort was discovered (e.g. job_a here, job_b next in the
        # round robin, then the abort) — those still get rebuilt, matching
        # the pre-lookahead behaviour. A wave admitted-but-never-dispatched
        # after the abort naturally has no successes at all (every job in it
        # goes straight to the abort-skip branch in `run()`), so
        # `_wave_has_success` alone already keeps it from reaching
        # `rebuild_wave` — no separate abort check needed here.
        if self._wave_has_success(wave_jobs):
            try:
                rebuild_wave(
                    wave_jobs,
                    self._wave_results,
                    self._buffers,
                    self.rebuilders,
                    self.options,
                    self.summary.days_rebuilt,
                    _echo.echo_skip,
                )
            except Exception as exc:  # noqa: BLE001 -- any rebuild failure aborts the run
                self._fail_wave(wave_jobs, str(exc)[:500])
                self.summary.aborted = "clickhouse"
                self._current_abort_reason = _ABORT_REASONS["clickhouse"]

        self._finish_wave(wave_jobs)
        for key in self._wave_buffer_keys(wave_jobs):
            self._buffers.pop(key, None)

    def _wave_has_success(self, wave_jobs: list[ChunkJob]) -> bool:
        return any(
            (result := self._wave_results.get(id(job))) is not None and result.status == "success"
            for job in wave_jobs
        )

    def _wave_buffer_keys(self, wave_jobs: list[ChunkJob]) -> set[tuple[str, str, date]]:
        return {(job.db, job.table, d) for job in wave_jobs for d in job.chunk.dates()}

    # -- per-job dispatch -----------------------------------------------

    def _process_job(self, job: ChunkJob) -> str | None:
        if job.pair in self._failed_pairs:
            self._record_result(job, "skipped", skip_reason="skipped (earlier chunk failed)")
            return None

        jid = id(job)
        started_at = self._job_started_at.get(jid)
        if started_at is None:
            started_at = self.now()
            self._job_started_at[jid] = started_at
            self.loads.start_chunk(job.load(self.run_id), started_at)

        outcome = process_job(
            job,
            client=self.client,
            policy_factory=self.policy_factory,
            run_id=self.run_id,
            now=self.now,
            clock=self.scheduler.clock,
            record_call=self.scheduler.record_call,
            started_at=started_at,
            currency=self.currency_for(job.app_id),
        )

        if outcome.signal == DEFERRED:
            self._pending_api_calls[jid] = self._pending_api_calls.get(jid, 0) + outcome.api_calls
            # Either `defer` re-queues the job (nothing terminal yet — the
            # accumulated api_calls/started_at above wait for its eventual
            # success/failure) or it's out of deferrals and the scheduler
            # already recorded a SkippedJob; `_drain_scheduler_skips` (called
            # right after this in `run()`) picks that up and consumes the
            # same accumulated state either way.
            self.scheduler.defer(job, outcome.retry_after or 0.0)
            return None

        if outcome.signal == TRANSIENT_DEFERRED:
            self._pending_api_calls[jid] = self._pending_api_calls.get(jid, 0) + outcome.api_calls
            self._pending_transient[jid] = (outcome.http_status, outcome.error or "")
            if self.scheduler.defer_transient(job):
                # Re-queued — nothing terminal yet, same as a DEFERRED
                # rate-limit job; the accumulated state above waits for its
                # eventual success/exhaustion.
                return None
            # Exhausted: unlike a rate-limit deferral, `defer_transient`
            # doesn't record a SkippedJob itself (a transient-exhausted job
            # is a genuine failure, not a quota skip) — build the terminal
            # result here and fall through to the common handling below,
            # which pops the accumulated `_pending_api_calls` for us.
            http_status, error = self._pending_transient.pop(jid, (None, ""))
            outcome = FetchOutcome(
                JobResult(
                    job=job,
                    status="failed",
                    error=error,
                    http_status=http_status,
                    started_at=started_at,
                    finished_at=self.now(),
                ),
                # A 403 without the quota marker is a TransientError (see
                # afly.appsflyer.errors.TransientError's docstring) that
                # RetryPolicy's inline path escalates to AuthError once
                # retries are exhausted — reproduce that parity here too, so
                # exhausting the deferred schedule on one aborts the run the
                # same way an inline exhaustion would.
                signal=AUTH_ABORT if http_status == 403 else None,
            )

        result = outcome.result
        assert result is not None  # every non-DEFERRED signal carries a terminal result
        result.api_calls += self._pending_api_calls.pop(jid, 0)
        self._pending_transient.pop(jid, None)
        self._job_started_at.pop(jid, None)
        self._wave_results[jid] = result
        self._wave_tracker.note_terminal(job)

        if result.status == "success":
            for header in outcome.unknown_headers:
                self._warn_unknown_header(header)
            for row in outcome.rows:
                self._buffers.setdefault((job.db, job.table, row["date"]), []).append(row)
            _echo.echo_success(self.echo, job, result.rows, result.api_calls)
        elif result.status == "failed":
            self._failed_pairs.add(job.pair)
            _echo.echo_fail(job, result.error or "")
        else:
            _echo.echo_skip(job, result.skip_reason or "")

        return outcome.signal

    # -- wave/result bookkeeping -----------------------------------------

    def _record_result(
        self,
        job: ChunkJob,
        status: str,
        *,
        rows: int = 0,
        api_calls: int = 0,
        http_status: int | None = None,
        error: str | None = None,
        skip_reason: str | None = None,
        started_at: datetime | None = None,
    ) -> None:
        now = self.now()
        self._wave_results[id(job)] = JobResult(
            job=job,
            status=status,
            rows=rows,
            api_calls=api_calls,
            http_status=http_status,
            error=error,
            skip_reason=skip_reason,
            started_at=started_at or now,
            finished_at=now,
        )
        self._wave_tracker.note_terminal(job)

    def _drain_scheduler_skips(self) -> None:
        new_skips = self.scheduler.skipped[self._skip_cursor :]
        self._skip_cursor = len(self.scheduler.skipped)
        for sj in new_skips:
            jid = id(sj.job)
            # A job skipped after exhausting its rate-limit deferral budget
            # (see `defer`) carries api_calls/started_at accumulated across
            # its earlier attempts; every other skip reason (budget/max-calls/
            # app-exhaustion before a first attempt) never dispatched, so
            # these are simply absent and `_record_result` falls back to now().
            api_calls = self._pending_api_calls.pop(jid, 0)
            started_at = self._job_started_at.pop(jid, None)
            self._record_result(
                sj.job, "skipped", api_calls=api_calls, skip_reason=sj.reason, started_at=started_at
            )
            _echo.echo_skip(sj.job, sj.reason)

    def _fail_wave(self, wave_jobs: list[ChunkJob], error: str) -> None:
        for job in wave_jobs:
            existing = self._wave_results.get(id(job))
            started_at = existing.started_at if existing else None
            self._record_result(job, "failed", error=error, started_at=started_at)

    def _finish_wave(self, wave_jobs: list[ChunkJob]) -> None:
        for job in wave_jobs:
            jid = id(job)
            result = self._wave_results.get(jid)
            if result is None:
                now = self.now()
                result = JobResult(
                    job=job,
                    status="skipped",
                    skip_reason="not processed",
                    started_at=now,
                    finished_at=now,
                )
                self._wave_results[jid] = result
                self._wave_tracker.note_terminal(job)

            self._accumulate_extract_stats(result)
            self.loads.finish_chunk(
                job.load(self.run_id),
                result.status,
                started_at=result.started_at or self.now(),
                finished_at=result.finished_at or self.now(),
                rows=result.rows,
                api_calls=result.api_calls,
                http_status=result.http_status,
                error=result.error,
                skip_reason=result.skip_reason,
            )
            self.summary.jobs.append(job_result_to_dict(result))
            # Pop (not a blanket clear): other admitted-but-not-yet-front
            # waves may already have terminal results recorded here too, on
            # this same shared dict, since dispatch order isn't wave order.
            self._wave_results.pop(jid, None)

    def _accumulate_extract_stats(self, result: JobResult) -> None:
        stats = self._extract_stats.setdefault(
            result.job.extract.config.name,
            {
                "succeeded": 0,
                "failed": 0,
                "skipped": 0,
                "rows": 0,
                "api_calls": 0,
                "api_calls_long": 0,
            },
        )
        stats[_STAT_KEY[result.status]] += 1
        stats["rows"] += result.rows
        stats["api_calls"] += result.api_calls
        if result.job.is_long:
            stats["api_calls_long"] += result.api_calls

    def _finalize_extract_stats(self) -> None:
        for entry in self.summary.extracts:
            stats = self._extract_stats.get(entry["name"])
            if stats:
                entry.update(stats)

    def _warn_unknown_header(self, header: str) -> None:
        if header in self._warned_headers:
            return
        self._warned_headers.add(header)
        echo_warning(f"unknown CSV header: {header}")


__all__ = ["Executor", "LoadsRepoLike", "RebuilderLike"]
