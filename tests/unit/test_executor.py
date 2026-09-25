"""Unit tests for afly.run.executor.Executor — wave-by-wave fetch/parse/rebuild.

Uses FakeAppsFlyer (a duck-typed AppsFlyerClient, so the real fetch_report/
RetryPolicy/PullRequestSpec/parse_report all run unmodified) plus the
ClickHouse-layer fakes (FakeLoadsRepo/FakeRebuilder/ExplodingRebuilder) —
never a real network call or database.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime

import pytest

from afly.appsflyer.retry import RetryPolicy
from afly.config.project_config import QuotaConfig
from afly.run.executor import Executor
from afly.run.options import RunOptions
from afly.run.planner import ChunkJob, Plan
from afly.run.scheduler import QuotaScheduler
from afly.run.summary import RunSummary
from afly.run.windows import Chunk

from .run_fakes import (
    ExplodingRebuilder,
    FakeAppsFlyer,
    FakeClock,
    FakeLoadsRepo,
    FakeRebuilder,
    FakeSleep,
    make_loaded,
)

_NOW = datetime(2026, 9, 22, 12, 0, 0)
_REPORT_TYPE = "geo_by_date_report"


def _job(
    extract_name: str,
    app_id: str,
    from_date: date,
    to_date: date,
    *,
    index: int = 0,
    is_long: bool = False,
    on_empty: str = "skip",
    db: str = "marts",
    table: str = "t",
    report_type: str = _REPORT_TYPE,
) -> ChunkJob:
    extract = make_loaded(extract_name, report_type=report_type, on_empty=on_empty, table=table)
    chunk = Chunk(index=index, from_date=from_date, to_date=to_date)
    return ChunkJob(
        extract=extract, app_id=app_id, chunk=chunk, is_long=is_long, db=db, table=table
    )


def _executor(
    jobs: list[ChunkJob],
    *,
    client: FakeAppsFlyer,
    loads: FakeLoadsRepo,
    rebuilders: dict[tuple[str, str], object],
    options: RunOptions | None = None,
    max_retries: int = 1,
    quota_max_retries: int | None = None,
    max_waves_in_flight: int = 2,
    short_call_interval_seconds: int = 0,
    clock: Callable[[], float] = lambda: 0.0,
    sleep: Callable[[float], None] = lambda s: None,
    currency_for: Callable[[str], str | None] = lambda app_id: None,
) -> tuple[Executor, RunSummary]:
    """``quota_max_retries`` defaults to ``max_retries`` (they're the same
    field, ``QuotaConfig.max_retries``/``RetryPolicy.max_retries``, but a
    RateLimitError test needs to control the scheduler's deferral budget
    independently of the RetryPolicy attempt count, which no longer matters
    for RateLimitError now that `defer_rate_limits=True` raises on the first
    hit regardless of it). ``max_waves_in_flight`` defaults to the
    ``QuotaConfig`` default (2) — most tests here exercise a single wave and
    are indifferent to it; lookahead-specific tests override it (and, for
    ``max_waves_in_flight=1``, are asserting the *old* strict order is still
    reproduced exactly)."""
    summary = RunSummary(selector="*")
    options = options or RunOptions(select="*")
    quota = QuotaConfig(
        short_call_interval_seconds=short_call_interval_seconds,
        min_gap_seconds=0.0,
        max_retries=quota_max_retries if quota_max_retries is not None else max_retries,
        max_waves_in_flight=max_waves_in_flight,
    )
    scheduler = QuotaScheduler(jobs, quota, clock=clock, sleep=sleep)
    plan = Plan(extracts=[], jobs=jobs)
    executor = Executor(
        plan=plan,
        scheduler=scheduler,
        client=client,  # type: ignore[arg-type]
        policy_factory=lambda: RetryPolicy(
            max_retries=max_retries, sleep=lambda s: None, defer_rate_limits=True
        ),
        loads=loads,  # type: ignore[arg-type]
        rebuilders=rebuilders,  # type: ignore[arg-type]
        run_id="run1",
        now=lambda: _NOW,
        options=options,
        summary=summary,
        currency_for=currency_for,
        echo=lambda s: None,
    )
    return executor, summary


def _finished(loads: FakeLoadsRepo, extract: str, app_id: str) -> object:
    matches = [f for f in loads.finished if f.load.extract == extract and f.load.app_id == app_id]
    assert (
        len(matches) == 1
    ), f"expected exactly one finish_chunk for {extract}/{app_id}, got {matches}"
    return matches[0]


# -- happy path -----------------------------------------------------------


@pytest.mark.unit
def test_happy_path_coverage_and_fresh_rows_per_day() -> None:
    day1, day2 = date(2026, 1, 1), date(2026, 1, 2)
    job_a = _job("standard", "app1", day1, day2)
    job_b = _job("standard", "app2", day1, day2)

    client = FakeAppsFlyer()
    client.script(_REPORT_TYPE, "app1", day1, day2, "Date,Installs\n2026-01-01,5\n2026-01-02,7\n")
    client.script(_REPORT_TYPE, "app2", day1, day2, "Date,Installs\n2026-01-01,3\n")

    loads = FakeLoadsRepo()
    rebuilder = FakeRebuilder()
    executor, summary = _executor(
        [job_a, job_b], client=client, loads=loads, rebuilders={("marts", "t"): rebuilder}
    )
    executor.run()

    calls_by_day = {c.day: c for c in rebuilder.rebuild_calls}
    assert calls_by_day.keys() == {day1, day2}
    assert calls_by_day[day1].coverage == {(day1, "standard", "app1"), (day1, "standard", "app2")}
    assert len(calls_by_day[day1].fresh_rows) == 2
    assert calls_by_day[day2].coverage == {(day2, "standard", "app1"), (day2, "standard", "app2")}
    assert len(calls_by_day[day2].fresh_rows) == 1  # only app1 had a day-2 row

    assert _finished(loads, "standard", "app1").status == "success"
    assert _finished(loads, "standard", "app1").rows == 2
    assert _finished(loads, "standard", "app2").rows == 1

    job_dicts = {j["app_id"]: j for j in summary.jobs}
    assert job_dicts["app1"]["status"] == "success"
    assert job_dicts["app2"]["rows"] == 1
    assert len(summary.days_rebuilt) == 2


@pytest.mark.unit
def test_currency_for_app_id_is_stamped_onto_rows() -> None:
    # No "(Sales in <CUR>)" header in this CSV -> nothing to detect from
    # headers, so the executor's currency_for(app_id) hint is all there is.
    day1 = date(2026, 1, 1)
    job = _job("standard", "app1", day1, day1)

    client = FakeAppsFlyer()
    client.script(_REPORT_TYPE, "app1", day1, day1, "Date,Installs\n2026-01-01,5\n")

    loads = FakeLoadsRepo()
    rebuilder = FakeRebuilder()
    executor, _summary = _executor(
        [job],
        client=client,
        loads=loads,
        rebuilders={("marts", "t"): rebuilder},
        currency_for=lambda app_id: {"app1": "EUR"}.get(app_id),
    )
    executor.run()

    fresh_rows = rebuilder.rebuild_calls[0].fresh_rows
    assert len(fresh_rows) == 1
    assert fresh_rows[0]["currency"] == "EUR"


@pytest.mark.unit
def test_two_identical_csv_rows_both_reach_the_rebuilder() -> None:
    """Regression for the no-dedup product requirement, end to end through
    the executor's own buffering (a plain list, appended to — not a set or a
    dict keyed by content): AppsFlyer can return two rows identical on every
    dimension within one pull, and both must survive all the way to the
    fresh_rows the (fake) writer receives."""
    day1 = date(2026, 1, 1)
    job = _job("standard", "app1", day1, day1)

    client = FakeAppsFlyer()
    client.script(
        _REPORT_TYPE,
        "app1",
        day1,
        day1,
        "Date,Installs\n2026-01-01,5\n2026-01-01,5\n",  # two fully identical rows
    )

    loads = FakeLoadsRepo()
    rebuilder = FakeRebuilder()
    executor, _summary = _executor(
        [job], client=client, loads=loads, rebuilders={("marts", "t"): rebuilder}
    )
    executor.run()

    assert _finished(loads, "standard", "app1").rows == 2
    fresh_rows = rebuilder.rebuild_calls[0].fresh_rows
    assert len(fresh_rows) == 2
    assert fresh_rows[0] == fresh_rows[1]


# -- pair invariant ---------------------------------------------------------


@pytest.mark.unit
def test_failed_chunk_excludes_pair_from_coverage_and_skips_later_chunk() -> None:
    day1 = date(2026, 1, 1)
    day2 = date(2026, 1, 2)
    job1 = _job("standard", "app1", day1, day1, index=0)
    job2 = _job("standard", "app1", day2, day2, index=1)  # same pair, later wave

    client = FakeAppsFlyer()
    client.script(_REPORT_TYPE, "app1", day1, day1, (500, "server error"))
    # job2 intentionally has no scripted response: if the executor tried to
    # fetch it, FakeAppsFlyer.get() raises AssertionError.

    loads = FakeLoadsRepo()
    rebuilder = FakeRebuilder()
    executor, summary = _executor(
        [job1, job2], client=client, loads=loads, rebuilders={("marts", "t"): rebuilder}
    )
    executor.run()

    finished_for_pair = [f for f in loads.finished if f.load.app_id == "app1"]
    assert len(finished_for_pair) == 2
    statuses = sorted(f.status for f in finished_for_pair)
    assert statuses == ["failed", "skipped"]
    skipped = next(f for f in finished_for_pair if f.status == "skipped")
    assert skipped.skip_reason == "skipped (earlier chunk failed)"
    # No fetch attempted for job2's window.
    assert (_REPORT_TYPE, "app1", day2, day2) not in client.calls


# -- empty-response guard ----------------------------------------------------


@pytest.mark.unit
def test_empty_response_guarded_when_existing_rows_present() -> None:
    day1 = date(2026, 1, 1)
    job = _job("standard", "app1", day1, day1, on_empty="skip")

    client = FakeAppsFlyer()
    client.script(_REPORT_TYPE, "app1", day1, day1, "Date,Installs\n")  # header only, 0 rows

    loads = FakeLoadsRepo()
    rebuilder = FakeRebuilder(existing_rows={(day1, "standard", "app1"): 42})
    executor, summary = _executor(
        [job], client=client, loads=loads, rebuilders={("marts", "t"): rebuilder}
    )
    executor.run()

    finished = _finished(loads, "standard", "app1")
    assert finished.status == "skipped"
    assert finished.skip_reason is not None
    assert "42 existing rows kept" in finished.skip_reason
    assert rebuilder.rebuild_calls[0].coverage == set()  # pair excluded from coverage


@pytest.mark.unit
def test_empty_response_not_guarded_when_no_existing_rows() -> None:
    day1 = date(2026, 1, 1)
    job = _job("standard", "app1", day1, day1, on_empty="skip")

    client = FakeAppsFlyer()
    client.script(_REPORT_TYPE, "app1", day1, day1, "Date,Installs\n")

    loads = FakeLoadsRepo()
    rebuilder = FakeRebuilder()  # no existing rows scripted -> rows_for_pair() returns 0
    executor, summary = _executor(
        [job], client=client, loads=loads, rebuilders={("marts", "t"): rebuilder}
    )
    executor.run()

    finished = _finished(loads, "standard", "app1")
    assert finished.status == "success"
    assert finished.rows == 0
    assert rebuilder.rebuild_calls[0].coverage == {(day1, "standard", "app1")}


@pytest.mark.unit
def test_allow_empty_bypasses_guard_even_with_existing_rows() -> None:
    day1 = date(2026, 1, 1)
    job = _job("standard", "app1", day1, day1, on_empty="skip")

    client = FakeAppsFlyer()
    client.script(_REPORT_TYPE, "app1", day1, day1, "Date,Installs\n")

    loads = FakeLoadsRepo()
    rebuilder = FakeRebuilder(existing_rows={(day1, "standard", "app1"): 42})
    options = RunOptions(select="*", allow_empty=True)
    executor, summary = _executor(
        [job], client=client, loads=loads, rebuilders={("marts", "t"): rebuilder}, options=options
    )
    executor.run()

    finished = _finished(loads, "standard", "app1")
    assert finished.status == "success"
    assert rebuilder.rebuild_calls[0].coverage == {(day1, "standard", "app1")}


# -- AuthError abort ----------------------------------------------------


@pytest.mark.unit
def test_auth_error_aborts_run_but_rebuilds_days_already_fetched() -> None:
    day1 = date(2026, 1, 1)
    job_a = _job("e_a", "a1", day1, day1, index=0, table="t")
    job_b = _job("e_b", "b1", day1, day1, index=0, table="t")
    job_c = _job("e_c", "c1", day1, day1, index=0, table="t")  # same wave, must be skipped
    job_d = _job("e_d", "d1", day1, day1, index=1, table="t")  # later wave, must be skipped

    client = FakeAppsFlyer()
    client.script(_REPORT_TYPE, "a1", day1, day1, "Date,Installs\n2026-01-01,1\n")
    client.script(_REPORT_TYPE, "b1", day1, day1, (401, "unauthorized"))
    # job_c/job_d intentionally unscripted.

    loads = FakeLoadsRepo()
    rebuilder = FakeRebuilder()
    executor, summary = _executor(
        [job_a, job_b, job_c, job_d],
        client=client,
        loads=loads,
        rebuilders={("marts", "t"): rebuilder},
    )
    executor.run()

    assert summary.aborted == "auth"
    assert _finished(loads, "e_a", "a1").status == "success"
    assert _finished(loads, "e_b", "b1").status == "failed"
    assert _finished(loads, "e_c", "c1").status == "skipped"
    assert _finished(loads, "e_c", "c1").skip_reason == "aborted: authentication failed"
    assert _finished(loads, "e_d", "d1").status == "skipped"
    assert _finished(loads, "e_d", "d1").skip_reason == "aborted: authentication failed"

    # The day already fetched successfully (job_a) before the abort was
    # still rebuilt.
    assert len(rebuilder.rebuild_calls) == 1
    assert rebuilder.rebuild_calls[0].coverage == {(day1, "e_a", "a1")}

    # job_c/job_d were never fetched (would have raised AssertionError).
    assert ("geo_by_date_report", "c1", day1, day1) not in client.calls
    assert ("geo_by_date_report", "d1", day1, day1) not in client.calls


# -- ClickHouse rebuild failure -------------------------------------------


@pytest.mark.unit
def test_clickhouse_error_in_rebuild_fails_whole_wave_and_aborts() -> None:
    day1 = date(2026, 1, 1)
    day2 = date(2026, 1, 2)
    job_a = _job("e_a", "a1", day1, day1, index=0, table="t")
    job_b = _job("e_b", "b1", day1, day1, index=0, table="t")
    job_later = _job("e_c", "c1", day2, day2, index=1, table="t")  # later wave

    client = FakeAppsFlyer()
    client.script(_REPORT_TYPE, "a1", day1, day1, "Date,Installs\n2026-01-01,1\n")
    client.script(_REPORT_TYPE, "b1", day1, day1, "Date,Installs\n2026-01-01,2\n")
    # job_later intentionally unscripted: it must never be fetched.

    loads = FakeLoadsRepo()
    exploding = ExplodingRebuilder()
    executor, summary = _executor(
        [job_a, job_b, job_later],
        client=client,
        loads=loads,
        rebuilders={("marts", "t"): exploding},
    )
    executor.run()

    assert summary.aborted == "clickhouse"
    # Every job of the wave that hit the rebuild failure is marked failed —
    # even job_a/job_b, which fetched successfully.
    finished_a = _finished(loads, "e_a", "a1")
    finished_b = _finished(loads, "e_b", "b1")
    assert finished_a.status == "failed"
    assert finished_b.status == "failed"
    assert finished_a.error is not None and "boom" in finished_a.error

    finished_later = _finished(loads, "e_c", "c1")
    assert finished_later.status == "skipped"
    assert finished_later.skip_reason == "aborted: ClickHouse error"
    assert (_REPORT_TYPE, "c1", day2, day2) not in client.calls
    assert exploding.staging_prepared is False  # never asked to prepare (test-owned fake)


# -- rate limiting: deferred to the scheduler, not blocking other keys -----


@pytest.mark.unit
def test_rate_limit_on_long_job_skips_and_marks_app_exhausted_across_waves() -> None:
    """`quota_max_retries=0` means the scheduler's very first `defer` call is
    already over budget — one rate-limited attempt is enough to skip and
    exhaust the app, matching the old inline-retry test's expectations."""
    day1 = date(2026, 1, 1)
    day10 = date(2026, 1, 10)
    day20 = date(2026, 1, 20)
    job1 = _job("e_a", "appX", day1, day10, index=0, is_long=True)
    job2 = _job("e_a", "appX", day20, day20, index=1, is_long=True)

    client = FakeAppsFlyer()
    client.script(_REPORT_TYPE, "appX", day1, day10, (403, "Limit reached for daily_report quota"))
    # job2 intentionally unscripted: mark_app_exhausted must skip it before fetch.

    loads = FakeLoadsRepo()
    rebuilder = FakeRebuilder()
    executor, summary = _executor(
        [job1, job2],
        client=client,
        loads=loads,
        rebuilders={("marts", "t"): rebuilder},
        quota_max_retries=0,
    )
    executor.run()

    # Two chunks for the same pair -> two finish_chunk rows; find each by window.
    rows = [f for f in loads.finished if f.load.extract == "e_a" and f.load.app_id == "appX"]
    assert len(rows) == 2
    row1 = next(r for r in rows if r.load.from_date == day1)
    row2 = next(r for r in rows if r.load.from_date == day20)
    assert row1.status == "skipped"
    assert row1.skip_reason is not None and row1.skip_reason.startswith("quota: rate limited")
    assert row2.status == "skipped"
    assert row2.skip_reason is not None and "exhausted" in row2.skip_reason
    assert (_REPORT_TYPE, "appX", day20, day20) not in client.calls


@pytest.mark.unit
def test_rate_limited_job_deferred_other_apps_run_between_its_attempts() -> None:
    """A job rate-limited twice, then succeeding on its 3rd attempt: ends
    `success` with api_calls accumulated across all 3 attempts, start_chunk
    fired exactly once (not per attempt), and — the actual throughput fix —
    the other two apps' jobs were fetched *between* app a1's two failures
    rather than the whole run stalling on a1's backoff."""
    day1 = date(2026, 1, 1)
    job_a = _job("standard", "a1", day1, day1)
    job_b = _job("standard", "b1", day1, day1)
    job_c = _job("standard", "c1", day1, day1)

    client = FakeAppsFlyer()
    client.script(
        _REPORT_TYPE,
        "a1",
        day1,
        day1,
        [
            (403, "Limit reached for pull quota"),
            (403, "Limit reached for pull quota"),
            "Date,Installs\n2026-01-01,1\n",
        ],
    )
    client.script(_REPORT_TYPE, "b1", day1, day1, "Date,Installs\n2026-01-01,2\n")
    client.script(_REPORT_TYPE, "c1", day1, day1, "Date,Installs\n2026-01-01,3\n")

    loads = FakeLoadsRepo()
    rebuilder = FakeRebuilder()
    executor, summary = _executor(
        [job_a, job_b, job_c],
        client=client,
        loads=loads,
        rebuilders={("marts", "t"): rebuilder},
        max_retries=3,
    )
    executor.run()

    finished = _finished(loads, "standard", "a1")
    assert finished.status == "success"
    assert finished.api_calls == 3  # 2 rate-limited attempts + the successful one

    a1_started = [s for s in loads.started if s.load.app_id == "a1"]
    assert len(a1_started) == 1  # start_chunk fired once, not per re-dispatch

    a1_positions = [i for i, c in enumerate(client.calls) if c[1] == "a1"]
    assert len(a1_positions) == 3
    between_first_two_attempts = client.calls[a1_positions[0] + 1 : a1_positions[1]]
    assert {c[1] for c in between_first_two_attempts} == {"b1", "c1"}

    assert _finished(loads, "standard", "b1").status == "success"
    assert _finished(loads, "standard", "c1").status == "success"


# -- wave lookahead (max_waves_in_flight) -----------------------------------


@pytest.mark.unit
def test_lookahead_dispatches_next_wave_without_sleeping_on_a_deferred_job() -> None:
    """A deferred job in wave 1 must not stall wave 2's *other* keys — the
    actual throughput bug this feature fixes: with the old strict one-wave
    scheduler, a wave whose last remaining job was rate-limited slept in
    place with every other key idle, even though a whole next wave of ready
    work was sitting right behind it.

    Asserts the *relative order* of events (an interleaved log of fetches
    and sleeps), not just that some sleep eventually happened — a20's own
    eventual retry still legitimately waits out its cooldown once nothing
    else is left to dispatch, so "zero sleeps in the whole run" would be the
    wrong bar. The bar is: b1 fetched before that sleep, not after."""
    day1, day2 = date(2026, 1, 1), date(2026, 1, 2)
    job_a = _job("standard", "a1", day1, day1, index=0)  # wave 0
    job_b = _job("standard", "b1", day2, day2, index=1)  # wave 1, different key

    client = FakeAppsFlyer()
    client.script(
        _REPORT_TYPE,
        "a1",
        day1,
        day1,
        [(403, "Limit reached for pull quota"), "Date,Installs\n2026-01-01,1\n"],
    )
    client.script(_REPORT_TYPE, "b1", day2, day2, "Date,Installs\n2026-01-02,1\n")

    events: list[str] = []
    orig_get = client.get

    def spy_get(path: str, params: dict[str, object] | None = None, accept: str = "application/json"):  # type: ignore[no-untyped-def]
        app_id = path.split("/app/", 1)[1].split("/", 1)[0]
        events.append(f"fetch {app_id}")
        return orig_get(path, params=params, accept=accept)

    client.get = spy_get  # type: ignore[method-assign]

    clock = FakeClock()

    def spy_sleep(seconds: float) -> None:
        events.append(f"sleep {seconds}")
        clock.advance(seconds)

    loads = FakeLoadsRepo()
    rebuilder = FakeRebuilder()
    executor, summary = _executor(
        [job_a, job_b],
        client=client,
        loads=loads,
        rebuilders={("marts", "t"): rebuilder},
        max_waves_in_flight=2,
        short_call_interval_seconds=60,
        quota_max_retries=3,
        clock=clock,
        sleep=spy_sleep,
    )
    executor.run()

    assert events[0] == "fetch a1"  # a1's first (rate-limited) attempt
    assert events[1] == "fetch b1"  # b1 dispatched right after — no sleep in between
    assert "sleep 60.0" in events
    assert events.index("fetch b1") < events.index("sleep 60.0")
    assert _finished(loads, "standard", "a1").status == "success"
    assert _finished(loads, "standard", "b1").status == "success"


@pytest.mark.unit
def test_lookahead_rebuilds_wave_1_before_wave_2_and_only_once_each_is_complete() -> None:
    """Wave 2's job can finish (dispatch-wise) before wave 1's does — a
    deferred job in wave 1 outlives a same-round-trip wave-2 job on a
    different key — but the rebuild itself must still land in wave order:
    wave 1's day rebuilt first, wave 2's second, each only once every job of
    that wave is terminal."""
    day1, day2 = date(2026, 1, 1), date(2026, 1, 2)
    job_a = _job("standard", "a1", day1, day1, index=0)  # wave 0
    job_b = _job("standard", "b1", day1, day1, index=0)  # wave 0, deferred once
    job_c = _job("standard", "c1", day2, day2, index=1)  # wave 1, unrelated key

    client = FakeAppsFlyer()
    client.script(_REPORT_TYPE, "a1", day1, day1, "Date,Installs\n2026-01-01,1\n")
    client.script(
        _REPORT_TYPE,
        "b1",
        day1,
        day1,
        [(403, "Limit reached for pull quota"), "Date,Installs\n2026-01-01,2\n"],
    )
    client.script(_REPORT_TYPE, "c1", day2, day2, "Date,Installs\n2026-01-02,3\n")

    loads = FakeLoadsRepo()
    rebuilder = FakeRebuilder()
    clock = FakeClock()
    sleep = FakeSleep(clock)
    executor, summary = _executor(
        [job_a, job_b, job_c],
        client=client,
        loads=loads,
        rebuilders={("marts", "t"): rebuilder},
        max_waves_in_flight=2,
        short_call_interval_seconds=30,
        quota_max_retries=3,
        clock=clock,
        sleep=sleep,
    )
    executor.run()

    # c1 (wave 1) necessarily finished its only attempt before b1's (wave 0)
    # retry landed — b1's cooldown is what the sleep below waits out — yet
    # the rebuild order is still wave-ordered: day1 (wave 0) before day2
    # (wave 1).
    assert sleep.calls == [30.0]
    assert [call.day for call in rebuilder.rebuild_calls] == [day1, day2]
    assert rebuilder.rebuild_calls[0].coverage == {
        (day1, "standard", "a1"),
        (day1, "standard", "b1"),
    }
    assert rebuilder.rebuild_calls[1].coverage == {(day2, "standard", "c1")}
    assert _finished(loads, "standard", "b1").status == "success"
    assert _finished(loads, "standard", "c1").status == "success"


@pytest.mark.unit
def test_lookahead_pair_rule_blocks_later_wave_until_earlier_chunk_terminal() -> None:
    """The pair invariant across waves: a later chunk of the same
    ``(extract, app)`` pair must never dispatch while an earlier chunk of
    that pair is still pending/deferred — otherwise a later chunk that
    succeeds before an earlier one's eventual failure would leave rows that
    should have been discarded. Here the wave-0 chunk is rate-limited once,
    then fails outright; the wave-1 chunk of the *same pair* must wait for
    that, then be skipped as "earlier chunk failed" without ever being
    fetched."""
    day1, day2 = date(2026, 1, 1), date(2026, 1, 2)
    job1 = _job("standard", "app1", day1, day1, index=0)
    job2 = _job("standard", "app1", day2, day2, index=1)  # same pair, later wave

    client = FakeAppsFlyer()
    client.script(
        _REPORT_TYPE,
        "app1",
        day1,
        day1,
        [(403, "Limit reached for pull quota"), (400, "bad app id")],
    )
    # job2 intentionally has no scripted response: if the pair-barrier ever
    # let it dispatch before job1 is terminal, FakeAppsFlyer.get() raises.

    loads = FakeLoadsRepo()
    rebuilder = FakeRebuilder()
    clock = FakeClock()
    sleep = FakeSleep(clock)
    executor, summary = _executor(
        [job1, job2],
        client=client,
        loads=loads,
        rebuilders={("marts", "t"): rebuilder},
        max_waves_in_flight=2,
        short_call_interval_seconds=60,
        quota_max_retries=3,
        clock=clock,
        sleep=sleep,
    )
    executor.run()

    # 1st wait: job1's own deferral; 2nd: the key's spacing after job1's failed call —
    # a failed request is a real AppsFlyer call and counts against the per-key limit.
    assert sleep.calls == [60.0, 60.0]
    assert (_REPORT_TYPE, "app1", day2, day2) not in client.calls

    rows = [f for f in loads.finished if f.load.extract == "standard" and f.load.app_id == "app1"]
    assert len(rows) == 2
    row1 = next(r for r in rows if r.load.from_date == day1)
    row2 = next(r for r in rows if r.load.from_date == day2)
    assert row1.status == "failed"
    row2_reason = row2.skip_reason
    assert row2.status == "skipped"
    assert row2_reason == "skipped (earlier chunk failed)"


@pytest.mark.unit
def test_max_waves_in_flight_one_reproduces_strict_wave_order() -> None:
    """`max_waves_in_flight=1` is documented to reproduce the pre-lookahead
    behaviour exactly: every job of wave 0 dispatches (and wave 0 rebuilds)
    before wave 1's job is even admitted to the scheduler."""
    day1, day2 = date(2026, 1, 1), date(2026, 1, 2)
    job_a = _job("standard", "a1", day1, day1, index=0)
    job_b = _job("standard", "b1", day1, day1, index=0)
    job_c = _job("standard", "c1", day2, day2, index=1)

    client = FakeAppsFlyer()
    client.script(_REPORT_TYPE, "a1", day1, day1, "Date,Installs\n2026-01-01,1\n")
    client.script(_REPORT_TYPE, "b1", day1, day1, "Date,Installs\n2026-01-01,2\n")
    client.script(_REPORT_TYPE, "c1", day2, day2, "Date,Installs\n2026-01-02,3\n")

    loads = FakeLoadsRepo()
    rebuilder = FakeRebuilder()
    executor, summary = _executor(
        [job_a, job_b, job_c],
        client=client,
        loads=loads,
        rebuilders={("marts", "t"): rebuilder},
        max_waves_in_flight=1,
    )
    executor.run()

    # c1 (wave 1) is dispatched strictly after both wave-0 jobs — the whole
    # point of `max_waves_in_flight=1`.
    call_apps = [c[1] for c in client.calls]
    assert call_apps.index("c1") > call_apps.index("a1")
    assert call_apps.index("c1") > call_apps.index("b1")
    assert [call.day for call in rebuilder.rebuild_calls] == [day1, day2]
    assert rebuilder.rebuild_calls[0].coverage == {
        (day1, "standard", "a1"),
        (day1, "standard", "b1"),
    }
    assert rebuilder.rebuild_calls[1].coverage == {(day2, "standard", "c1")}


# -- unknown header warning ------------------------------------------------


@pytest.mark.unit
def test_unknown_header_warns_once(capsys: pytest.CaptureFixture[str]) -> None:
    day1 = date(2026, 1, 1)
    job_a = _job("e_a", "a1", day1, day1)
    job_b = _job("e_b", "b1", day1, day1)

    client = FakeAppsFlyer()
    client.script(_REPORT_TYPE, "a1", day1, day1, "Date,Weirdo\n2026-01-01,5\n")
    client.script(_REPORT_TYPE, "b1", day1, day1, "Date,Weirdo\n2026-01-01,9\n")

    loads = FakeLoadsRepo()
    rebuilder = FakeRebuilder()
    executor, summary = _executor(
        [job_a, job_b], client=client, loads=loads, rebuilders={("marts", "t"): rebuilder}
    )
    executor.run()

    err = capsys.readouterr().err
    assert err.count("unknown CSV header: Weirdo") == 1


@pytest.mark.unit
def test_failed_chunk_records_status_calls_and_appsflyer_message() -> None:
    """A failed chunk's _afly_loads row must say what AppsFlyer answered: the
    live backfill of 2026-09-24 recorded `http_status=None, api_calls=0` and a
    bare "rejected (status 416)" for real failed calls, which hid the cause."""
    day1 = date(2026, 1, 1)
    job = _job("standard", "app1", day1, day1)

    client = FakeAppsFlyer()
    client.script(_REPORT_TYPE, "app1", day1, day1, (400, "Invalid date range"))

    loads = FakeLoadsRepo()
    executor, _summary = _executor(
        [job], client=client, loads=loads, rebuilders={("marts", "t"): FakeRebuilder()}
    )
    executor.run()

    [finished] = loads.finished
    assert finished.status == "failed"
    assert finished.http_status == 400
    assert finished.api_calls == 1
    assert "Invalid date range" in (finished.error or "")
