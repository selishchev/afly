"""QuotaScheduler — hands out plan-wave jobs in AppsFlyer-rate-limit order.

Drives two independent constraints AppsFlyer imposes on the Pull API:

- a **per-minute** throttle, scoped per ``(app_id, report_type)`` — modeled
  as ``next_allowed[key]`` plus a small global ``min_gap_seconds`` between
  any two calls at all;
- a **daily** long-call budget, scoped per account and per app — checked
  right before a long job would be handed out, not reserved ahead of time.

Callers (the ``Executor``) admit :class:`~afly.run.planner.Plan` waves one at
a time, in wave order, via :meth:`load_wave` — but, unlike the old strict
design, more than one wave can be admitted (not yet drained) at once: the
executor's own ``max_waves_in_flight``-sized lookahead window decides how
many. This is what lets a rate-limited job in wave *N* cool down without
idling keys that are ready in wave *N+1* — the old design only ever knew
about a single wave, so once its last job was deferred there was nothing
else to dispatch and the whole run blocked on the sleep. :meth:`next_job`
draws from *every* currently-admitted wave, oldest first, and keeps
dispatching until every admitted job is terminal or the run's stop
conditions kick in.

A pair's chunks must still be handed out in wave order — a later chunk must
never be fetched while an earlier chunk of the same ``(extract, app)`` pair
is still pending or cooling down from a rate limit, otherwise a later chunk
that succeeds before an earlier one's eventual failure would leave fetched
rows that should have been discarded (see ``Executor._failed_pairs``). Since
a pair always maps to exactly one scheduler key (``ChunkJob.key`` is a
function of ``ChunkJob.pair``'s app id and the extract's report type — see
``planner.ChunkJob``), this reduces to a single invariant enforced entirely
within :meth:`_enqueue`: each key's queue is kept sorted by admission order
(the wave/batch a job was loaded in), so :meth:`next_job` always serves a
key's earliest-wave job first, even one just re-queued by :meth:`defer`.

A live AppsFlyer rate-limit hit (as opposed to the two budgets above, which
are checked before a call is even made) is handled via :meth:`defer`: the
job goes back into its own key's queue with a delay, instead of the caller
sleeping in place — see that method's docstring for why.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from afly.config.project_config import QuotaConfig
from afly.run.planner import ChunkJob

_ACCOUNT_SCOPE = "account"


@dataclass
class SkippedJob:
    job: ChunkJob
    reason: str


class QuotaScheduler:
    def __init__(
        self,
        jobs: list[ChunkJob],
        quota: QuotaConfig,
        *,
        account_used: int = 0,
        app_used: dict[str, int] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_calls: int | None = None,
        max_minutes: int | None = None,
    ) -> None:
        self.jobs = list(jobs)
        self.quota = quota
        self.clock = clock
        self.sleep = sleep
        self.max_calls = max_calls
        self.max_minutes = max_minutes

        self.account_used = account_used
        self.app_used: dict[str, int] = dict(app_used or {})
        self._account_limit = quota.account_long_calls_per_day - quota.reserve_long_calls
        self._app_limit = quota.app_long_calls_per_day - quota.reserve_long_calls

        self.total_calls = 0
        self.skipped: list[SkippedJob] = []
        self.warnings: list[str] = []
        self._warned_scopes: set[str] = set()
        self._exhausted_apps: set[str] = set()

        self._start_time = clock()
        self._stop_reason: str | None = None

        self._wave_keys: dict[tuple[str, str], deque[ChunkJob]] = {}
        self._wave_key_order: deque[tuple[str, str]] = deque()
        self._next_allowed: dict[tuple[str, str], float] = {}
        self._last_call_at: float | None = None
        self._deferral_counts: dict[int, int] = {}

        # Cross-wave lookahead bookkeeping: each `load_wave` call is one
        # admission batch, numbered in call order. `_job_batch` records which
        # batch a job (still queued, or re-queued by `defer`) belongs to, so
        # `_enqueue` can keep every key's queue sorted oldest-batch-first —
        # see the module docstring for why that's the whole pair-ordering
        # invariant.
        self._batch_counter = 0
        self._job_batch: dict[int, int] = {}

    @property
    def stop_reason(self) -> str | None:
        """Why the scheduler stopped handing out jobs (``max-calls``/``max-minutes``), if it has."""
        return self._stop_reason

    # -- wave lifecycle ---------------------------------------------------

    def load_wave(self, jobs_of_wave: list[ChunkJob]) -> None:
        """Admit one more wave's jobs, round-robin grouped by ``job.key``.

        Call once per wave, in ascending wave order. Jobs already admitted by
        an earlier call and not yet dispatched (or cooling down after a
        :meth:`defer`) are left untouched — this call only *adds* to the
        queues, it never resets them — so the caller controls how many waves
        are simultaneously in flight simply by how far ahead of
        :meth:`next_job` draining it calls this.

        If a stop condition (max-calls/minutes, or a prior app exhaustion)
        already applies, the wave's jobs are skipped immediately rather than
        queued — matches "every remaining job (this wave + later)" from the
        stop-condition contract.
        """
        batch = self._batch_counter
        self._batch_counter += 1

        if self._stop_reason is None:
            self._check_limits()

        for job in jobs_of_wave:
            self._job_batch[id(job)] = batch
            if self._stop_reason is not None:
                self._skip(job, self._stop_reason)
                continue
            if job.is_long and job.app_id in self._exhausted_apps:
                self._skip(job, _app_exhausted_reason())
                continue
            self._enqueue(job)

    def next_job(self) -> ChunkJob | None:
        """The next ready job across every admitted wave, sleeping only when
        every pending key is throttled. ``None`` once every admitted job is
        dispatched (or, under a stop condition, drained/skipped)."""
        while True:
            self._purge_empty_keys()
            if not self._wave_key_order:
                return None

            if self._stop_reason is None:
                self._check_limits()
            if self._stop_reason is not None:
                self._drain_wave(self._stop_reason)
                return None

            ready_key, wait = self._find_ready_key()
            if ready_key is None:
                assert wait is not None
                self.sleep(wait)
                continue

            job = self._wave_keys[ready_key].popleft()

            if job.is_long:
                reason = self._long_budget_reason(job.app_id)
                if reason is not None:
                    self._skip(job, reason)
                    continue

            return job

    # -- call bookkeeping ---------------------------------------------------

    def record_call(self, job: ChunkJob, api_calls: int, at: float) -> None:
        """Record that *job* actually made *api_calls* HTTP calls, completing at *at*."""
        self._next_allowed[job.key] = at + self.quota.short_call_interval_seconds
        self._last_call_at = at
        self.total_calls += api_calls
        if job.is_long:
            self.account_used += 1
            self.app_used[job.app_id] = self.app_used.get(job.app_id, 0) + 1

    def defer(self, job: ChunkJob, delay_seconds: float) -> bool:
        """Send a rate-limited *job* back to the scheduler instead of retrying inline.

        This is what keeps one throttled key from blocking every other key:
        rather than sleeping in place (the old ``RetryPolicy`` behaviour),
        the job goes back into its own key's queue (re-sorted by admission
        batch — see ``_enqueue`` — so a later-wave sibling of the same pair
        already queued behind it still waits its turn) with
        ``next_allowed[job.key]`` pushed out, so :meth:`next_job` keeps
        handing out other ready keys in the meantime and only sleeps once
        every pending key is throttled.

        The delay escalates linearly per deferral of *this job*
        (``max(delay_seconds, quota.short_call_interval_seconds) *
        deferral_count`` — 60s, 120s, 180s, ... at the defaults), mirroring
        the old inline ``RetryPolicy`` backoff schedule so operators see the
        same pacing, just non-blocking now.

        Gives up after ``quota.max_retries`` deferrals: the job is moved to
        ``skipped`` (reusing the same ``"quota: rate limited after N
        attempts"`` message the old inline path produced) and, if it was a
        long job, :meth:`mark_app_exhausted` is applied — a key that keeps
        getting rate-limited on long calls is treated the same as an
        AppsFlyer-reported quota exhaustion.

        Returns ``True`` if the job was re-queued, ``False`` if it was
        skipped instead (the caller should not expect to see this job again
        via :meth:`next_job`).
        """
        jid = id(job)
        count = self._deferral_counts.get(jid, 0)
        if count >= self.quota.max_retries:
            self._deferral_counts.pop(jid, None)
            self._skip(job, f"quota: rate limited after {count + 1} attempts")
            if job.is_long:
                self.mark_app_exhausted(job.app_id)
            return False

        count += 1
        self._deferral_counts[jid] = count
        wait = max(delay_seconds or 0.0, self.quota.short_call_interval_seconds) * count
        ready_at = self.clock() + wait
        current = self._next_allowed.get(job.key, float("-inf"))
        self._next_allowed[job.key] = max(current, ready_at)
        self._enqueue(job)
        return True

    def mark_app_exhausted(self, app_id: str) -> None:
        """Stop handing out further long jobs for *app_id* (e.g. after repeated rate limits).

        Applies immediately to every currently-admitted wave's queue and
        persists for every later wave.
        """
        self._exhausted_apps.add(app_id)
        for key in list(self._wave_key_order):
            queue = self._wave_keys.get(key)
            if not queue:
                continue
            remaining: deque[ChunkJob] = deque()
            for job in queue:
                if job.is_long and job.app_id == app_id:
                    self._skip(job, _app_exhausted_reason())
                else:
                    remaining.append(job)
            self._wave_keys[key] = remaining

    # -- internals ----------------------------------------------------------

    def _enqueue(self, job: ChunkJob) -> None:
        if job.key not in self._wave_keys:
            self._wave_keys[job.key] = deque()
        if job.key not in self._wave_key_order:
            # Covers both a brand-new key and one `_purge_empty_keys` already
            # dropped from the round-robin order (the `defer` re-enqueue
            # case) — either way it needs to be back in rotation.
            self._wave_key_order.append(job.key)

        queue = self._wave_keys[job.key]
        batch = self._job_batch[id(job)]
        # Keep the queue sorted ascending by admission batch (wave order). A
        # fresh admission from `load_wave` always carries the newest (largest)
        # batch, so a plain append would already be sorted for that case —
        # but `defer` re-enqueues a job whose batch can be *smaller* than
        # jobs already queued behind it (a later-wave sibling of the same
        # pair, admitted into the lookahead window while this one was
        # cooling down). Appending it there would let that later-wave job
        # dispatch first, breaking the pair invariant a later chunk must
        # never run before an earlier chunk of the same pair is terminal
        # (module docstring). A pair always maps to one key (`ChunkJob.key`
        # is a function of `ChunkJob.pair`'s app id and the extract's own
        # report type), so keeping this one queue batch-sorted is the whole
        # fix — no separate cross-wave pair tracking needed. Cheap at this
        # scale (a handful of waves' worth of jobs per key).
        idx = len(queue)
        for i, existing in enumerate(queue):
            if self._job_batch[id(existing)] > batch:
                idx = i
                break
        queue.insert(idx, job)

    def _purge_empty_keys(self) -> None:
        self._wave_key_order = deque(k for k in self._wave_key_order if self._wave_keys.get(k))

    def _earliest_allowed(self, key: tuple[str, str]) -> float:
        per_key = self._next_allowed.get(key, float("-inf"))
        global_gap = (
            self._last_call_at + self.quota.min_gap_seconds
            if self._last_call_at is not None
            else float("-inf")
        )
        return max(per_key, global_gap)

    def _find_ready_key(self) -> tuple[tuple[str, str] | None, float | None]:
        """Scan keys front-to-back; return the first ready one (moved to the
        back of the queue, for fairness — the classic round-robin rotation),
        or (None, minimal wait) if every key is currently throttled.

        Scanning the front of the queue rather than an integer offset avoids
        a stale-index bug: an integer round-robin pointer desyncs the moment
        ``_purge_empty_keys`` removes a key ahead of it, silently skipping
        whichever key shifts into that slot.
        """
        now = self.clock()
        min_wait: float | None = None
        for key in self._wave_key_order:
            wait = self._earliest_allowed(key) - now
            if wait <= 0:
                self._wave_key_order.remove(key)
                self._wave_key_order.append(key)
                return key, None
            if min_wait is None or wait < min_wait:
                min_wait = wait
        return None, min_wait

    def _long_budget_reason(self, app_id: str) -> str | None:
        if app_id in self._exhausted_apps:
            return _app_exhausted_reason()
        if self.account_used + 1 > self._account_limit:
            self._warn_once(_ACCOUNT_SCOPE, "quota: daily long-call budget exhausted (account)")
            return "quota: daily long-call budget exhausted (account)"
        if self.app_used.get(app_id, 0) + 1 > self._app_limit:
            self._warn_once(
                f"app:{app_id}", f"quota: daily long-call budget exhausted (app {app_id})"
            )
            return f"quota: daily long-call budget exhausted (app {app_id})"
        return None

    def _warn_once(self, scope: str, message: str) -> None:
        if scope not in self._warned_scopes:
            self._warned_scopes.add(scope)
            self.warnings.append(message)

    def _check_limits(self) -> None:
        if self.max_calls is not None and self.total_calls >= self.max_calls:
            self._stop_reason = "max-calls reached"
            return
        if self.max_minutes is not None:
            elapsed_minutes = (self.clock() - self._start_time) / 60
            if elapsed_minutes >= self.max_minutes:
                self._stop_reason = "max-minutes reached"

    def _drain_wave(self, reason: str) -> None:
        for key in list(self._wave_key_order):
            queue = self._wave_keys.pop(key, None)
            if not queue:
                continue
            for job in queue:
                self._skip(job, reason)
        self._wave_key_order = deque()

    def _skip(self, job: ChunkJob, reason: str) -> None:
        self.skipped.append(SkippedJob(job=job, reason=reason))


def _app_exhausted_reason() -> str:
    return "quota: app exhausted after repeated rate limiting"


__all__ = ["QuotaScheduler", "SkippedJob"]
