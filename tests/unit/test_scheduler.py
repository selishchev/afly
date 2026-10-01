"""Unit tests for afly.run.scheduler.QuotaScheduler — deterministic under a fake clock."""

from __future__ import annotations

from datetime import date

import pytest

from afly.config.project_config import QuotaConfig
from afly.run.planner import ChunkJob
from afly.run.scheduler import QuotaScheduler
from afly.run.windows import Chunk

from .run_fakes import FakeClock, FakeSleep, make_loaded

_SHORT_CHUNK = Chunk(index=0, from_date=date(2026, 1, 1), to_date=date(2026, 1, 1))
_LONG_CHUNK = Chunk(index=0, from_date=date(2026, 1, 1), to_date=date(2026, 1, 5))


def _short_job(app_id: str, report_type: str = "geo_by_date_report") -> ChunkJob:
    extract = make_loaded(f"e_{report_type}_{app_id}", report_type=report_type)
    return ChunkJob(
        extract=extract, app_id=app_id, chunk=_SHORT_CHUNK, is_long=False, db="marts", table="t"
    )


def _long_job(app_id: str) -> ChunkJob:
    extract = make_loaded(f"e_long_{app_id}")
    return ChunkJob(
        extract=extract, app_id=app_id, chunk=_LONG_CHUNK, is_long=True, db="marts", table="t"
    )


def _run_all(scheduler: QuotaScheduler, wave: list[ChunkJob]) -> list[ChunkJob]:
    """Drain a wave via next_job()/record_call(), returning jobs in dispatch order."""
    scheduler.load_wave(wave)
    order: list[ChunkJob] = []
    while True:
        job = scheduler.next_job()
        if job is None:
            return order
        order.append(job)
        scheduler.record_call(job, 1, scheduler.clock())


def _quota(**overrides: object) -> QuotaConfig:
    base: dict[str, object] = {
        "short_call_interval_seconds": 60,
        "long_call_min_days": 3,
        "account_long_calls_per_day": 120,
        "app_long_calls_per_day": 24,
        "reserve_long_calls": 0,
        "min_gap_seconds": 0.0,
        "max_retries": 5,
        # Deterministic by default — a QuotaScheduler built with the real
        # QuotaConfig default (0.25) would jitter `defer()`'s wait with
        # `random.random()`, breaking the exact-wait assertions throughout
        # this file. Tests that specifically exercise jitter override this.
        "retry_jitter": 0.0,
    }
    base.update(overrides)
    return QuotaConfig(**base)  # type: ignore[arg-type]


@pytest.mark.unit
def test_eleven_keys_two_jobs_interleave_with_single_sleep_between_rounds() -> None:
    clock = FakeClock()
    sleep = FakeSleep(clock)
    jobs = []
    for i in range(11):
        app_id = str(i)
        jobs.append(_short_job(app_id))
        jobs.append(_short_job(app_id))

    scheduler = QuotaScheduler(jobs, _quota(), clock=clock, sleep=sleep)
    order = _run_all(scheduler, jobs)

    assert len(order) == 22
    # First 11 dispatched jobs are the 11 distinct keys' first job, in order —
    # no sleep needed since every key starts ready.
    first_round_keys = [j.key for j in order[:11]]
    assert len(set(first_round_keys)) == 11
    # Exactly one sleep (60s) bridges the two rounds; no further sleeping.
    assert sleep.calls == [60.0]


@pytest.mark.unit
def test_single_key_sleeps_sixty_seconds_between_its_two_jobs() -> None:
    clock = FakeClock()
    sleep = FakeSleep(clock)
    jobs = [_short_job("1"), _short_job("1")]
    scheduler = QuotaScheduler(jobs, _quota(), clock=clock, sleep=sleep)
    order = _run_all(scheduler, jobs)

    assert len(order) == 2
    assert sleep.calls == [60.0]


@pytest.mark.unit
def test_min_gap_seconds_applies_globally_across_keys() -> None:
    clock = FakeClock()
    sleep = FakeSleep(clock)
    jobs = [_short_job("1"), _short_job("2")]
    scheduler = QuotaScheduler(jobs, _quota(min_gap_seconds=5.0), clock=clock, sleep=sleep)
    order = _run_all(scheduler, jobs)

    assert len(order) == 2
    # Different keys, but the global min-gap still forces a short sleep
    # before the second call.
    assert sleep.calls == [5.0]


@pytest.mark.unit
def test_account_budget_skips_long_job_over_limit() -> None:
    clock = FakeClock()
    quota = _quota(account_long_calls_per_day=1, app_long_calls_per_day=100)
    jobs = [_long_job("1"), _long_job("2")]
    scheduler = QuotaScheduler(jobs, quota, clock=clock, sleep=lambda s: None)
    order = _run_all(scheduler, jobs)

    assert len(order) == 1
    assert len(scheduler.skipped) == 1
    assert "account" in scheduler.skipped[0].reason
    assert scheduler.warnings  # warned once


@pytest.mark.unit
def test_app_budget_skips_long_job_over_limit() -> None:
    clock = FakeClock()
    sleep = FakeSleep(clock)
    quota = _quota(account_long_calls_per_day=100, app_long_calls_per_day=1)
    # Same app_id twice -> same scheduler key, so the 2nd job also has to
    # clear the per-key throttle before its budget check runs; FakeSleep
    # advances the fake clock so that wait resolves instead of hanging.
    jobs = [_long_job("app1"), _long_job("app1")]
    scheduler = QuotaScheduler(jobs, quota, clock=clock, sleep=sleep)
    order = _run_all(scheduler, jobs)

    assert len(order) == 1
    assert len(scheduler.skipped) == 1
    assert "app app1" in scheduler.skipped[0].reason


@pytest.mark.unit
def test_budget_warning_emitted_once_per_scope() -> None:
    clock = FakeClock()
    quota = _quota(account_long_calls_per_day=0, app_long_calls_per_day=100)
    jobs = [_long_job("1"), _long_job("2"), _long_job("3")]
    scheduler = QuotaScheduler(jobs, quota, clock=clock, sleep=lambda s: None)
    _run_all(scheduler, jobs)

    account_warnings = [w for w in scheduler.warnings if "account" in w]
    assert len(account_warnings) == 1
    assert len(scheduler.skipped) == 3


@pytest.mark.unit
def test_seeded_account_usage_counts_toward_budget() -> None:
    clock = FakeClock()
    quota = _quota(account_long_calls_per_day=2, app_long_calls_per_day=100)
    jobs = [_long_job("1")]
    scheduler = QuotaScheduler(jobs, quota, account_used=2, clock=clock, sleep=lambda s: None)
    order = _run_all(scheduler, jobs)

    assert order == []
    assert len(scheduler.skipped) == 1


@pytest.mark.unit
def test_seeded_app_usage_counts_toward_budget() -> None:
    clock = FakeClock()
    quota = _quota(account_long_calls_per_day=100, app_long_calls_per_day=1)
    jobs = [_long_job("app1")]
    scheduler = QuotaScheduler(jobs, quota, app_used={"app1": 1}, clock=clock, sleep=lambda s: None)
    order = _run_all(scheduler, jobs)

    assert order == []
    assert len(scheduler.skipped) == 1


@pytest.mark.unit
def test_mark_app_exhausted_skips_remaining_long_jobs_of_that_app_same_wave() -> None:
    clock = FakeClock()
    quota = _quota()
    jobs = [_long_job("app1"), _long_job("app1"), _long_job("app2")]
    scheduler = QuotaScheduler(jobs, quota, clock=clock, sleep=lambda s: None)
    scheduler.load_wave(jobs)

    first = scheduler.next_job()
    assert first is not None
    scheduler.record_call(first, 1, clock())
    scheduler.mark_app_exhausted("app1")

    remaining = []
    while (job := scheduler.next_job()) is not None:
        remaining.append(job)
        scheduler.record_call(job, 1, clock())

    assert [j.app_id for j in remaining] == ["app2"]
    assert any("exhausted" in sj.reason for sj in scheduler.skipped)


@pytest.mark.unit
def test_mark_app_exhausted_persists_to_later_waves() -> None:
    clock = FakeClock()
    quota = _quota()
    scheduler = QuotaScheduler([], quota, clock=clock, sleep=lambda s: None)
    scheduler.mark_app_exhausted("app1")

    wave = [_long_job("app1")]
    order = _run_all(scheduler, wave)
    assert order == []
    assert len(scheduler.skipped) == 1


@pytest.mark.unit
def test_max_calls_stops_and_skips_remaining_jobs() -> None:
    clock = FakeClock()
    quota = _quota()
    jobs = [_short_job("1"), _short_job("2"), _short_job("3")]
    scheduler = QuotaScheduler(jobs, quota, clock=clock, sleep=lambda s: None, max_calls=2)
    order = _run_all(scheduler, jobs)

    assert len(order) == 2
    assert scheduler.stop_reason == "max-calls reached"
    assert len(scheduler.skipped) == 1
    assert scheduler.skipped[0].reason == "max-calls reached"


@pytest.mark.unit
def test_max_calls_skips_a_later_wave_entirely() -> None:
    clock = FakeClock()
    quota = _quota()
    wave1 = [_short_job("1")]
    wave2 = [_short_job("2"), _short_job("3")]
    scheduler = QuotaScheduler(wave1 + wave2, quota, clock=clock, sleep=lambda s: None, max_calls=1)

    order1 = _run_all(scheduler, wave1)
    assert len(order1) == 1

    scheduler.load_wave(wave2)
    assert scheduler.next_job() is None
    assert len(scheduler.skipped) == 2
    assert all(sj.reason == "max-calls reached" for sj in scheduler.skipped)


@pytest.mark.unit
def test_max_minutes_stops_after_elapsed_time() -> None:
    clock = FakeClock()
    quota = _quota()
    jobs = [_short_job("1"), _short_job("2")]
    scheduler = QuotaScheduler(jobs, quota, clock=clock, sleep=lambda s: None, max_minutes=1)
    scheduler.load_wave(jobs)

    job = scheduler.next_job()
    assert job is not None
    scheduler.record_call(job, 1, clock())

    clock.advance(61)  # past the 1-minute budget
    assert scheduler.next_job() is None
    assert scheduler.stop_reason == "max-minutes reached"


@pytest.mark.unit
def test_defer_delays_only_its_own_key_other_keys_dispatch_immediately() -> None:
    clock = FakeClock()
    sleep = FakeSleep(clock)
    quota = _quota(short_call_interval_seconds=60)
    job_a, job_b, job_c = _short_job("a"), _short_job("b"), _short_job("c")
    jobs = [job_a, job_b, job_c]
    scheduler = QuotaScheduler(jobs, quota, clock=clock, sleep=sleep)
    scheduler.load_wave(jobs)

    first = scheduler.next_job()
    assert first is not None and first.app_id == "a"
    assert scheduler.defer(first, 0.0) is True
    assert sleep.calls == []  # deferring itself never sleeps

    # B and C are still ready — no sleep needed to reach them even though A
    # is now cooling down.
    second = scheduler.next_job()
    third = scheduler.next_job()
    assert {second.app_id, third.app_id} == {"b", "c"}  # type: ignore[union-attr]
    assert sleep.calls == []

    # Only once every other key is drained does A's own 60s delay matter.
    fourth = scheduler.next_job()
    assert fourth is not None and fourth.app_id == "a"
    assert sleep.calls == [60.0]


@pytest.mark.unit
def test_defer_escalates_linearly_then_skips_after_max_retries_and_exhausts_app() -> None:
    clock = FakeClock()
    sleep = FakeSleep(clock)
    quota = _quota(short_call_interval_seconds=60, max_retries=2)
    job = _long_job("app1")
    scheduler = QuotaScheduler([job], quota, clock=clock, sleep=sleep)
    scheduler.load_wave([job])

    got1 = scheduler.next_job()
    assert got1 is job
    assert scheduler.defer(job, 0.0) is True  # deferral #1: 60s * 1

    got2 = scheduler.next_job()
    assert got2 is job
    assert sleep.calls == [60.0]
    assert scheduler.defer(job, 0.0) is True  # deferral #2: 60s * 2

    got3 = scheduler.next_job()
    assert got3 is job
    assert sleep.calls == [60.0, 120.0]
    assert scheduler.defer(job, 0.0) is False  # 3rd hit exceeds max_retries=2 -> skip

    assert scheduler.next_job() is None
    assert len(scheduler.skipped) == 1
    assert scheduler.skipped[0].reason == "quota: rate limited after 3 attempts"

    # Long job -> the app is exhausted for the rest of the run, later waves included.
    later = _long_job("app1")
    scheduler.load_wave([later])
    assert scheduler.next_job() is None
    assert len(scheduler.skipped) == 2
    assert "exhausted" in scheduler.skipped[1].reason


@pytest.mark.unit
def test_defer_jitter_never_drops_below_nominal_wait() -> None:
    """``defer``'s escalating wait is jittered the same way RetryPolicy's own
    waits are (see test_retry.py) — it must never drop below the nominal
    ``max(delay, short_call_interval_seconds) * count`` value."""
    for r in (0.0, 0.5, 0.999):
        clock = FakeClock()
        sleep = FakeSleep(clock)
        quota = _quota(short_call_interval_seconds=60, retry_jitter=0.25)
        job = _long_job("app1")
        scheduler = QuotaScheduler([job], quota, clock=clock, sleep=sleep, rand=lambda r=r: r)
        scheduler.load_wave([job])

        first = scheduler.next_job()
        assert first is job
        assert scheduler.defer(job, 0.0) is True

        second = scheduler.next_job()
        assert second is job
        assert sleep.calls == [pytest.approx(60.0 * (1 + 0.25 * r))]
        assert sleep.calls[0] >= 60.0


@pytest.mark.unit
def test_record_call_increments_total_calls_by_api_calls() -> None:
    clock = FakeClock()
    quota = _quota()
    jobs = [_short_job("1")]
    scheduler = QuotaScheduler(jobs, quota, clock=clock, sleep=lambda s: None)
    scheduler.load_wave(jobs)
    job = scheduler.next_job()
    assert job is not None
    scheduler.record_call(job, 3, clock())
    assert scheduler.total_calls == 3
