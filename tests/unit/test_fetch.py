"""Unit tests for afly.run._fetch.process_job — the TransientError deferral path.

Uses FakeAppsFlyer (a duck-typed AppsFlyerClient) so the real
afly.appsflyer.pull_api.fetch_report/RetryPolicy/PullRequestSpec run
unmodified — only the HTTP transport is faked. Most of process_job's
branching is already exercised end to end via test_executor.py; this file
isolates the TRANSIENT_DEFERRED signal itself (api_calls/http_status/error,
record_call) independent of scheduler/executor bookkeeping.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from afly.appsflyer.retry import RetryPolicy
from afly.run._fetch import DEFERRED, TRANSIENT_DEFERRED, process_job
from afly.run.planner import ChunkJob
from afly.run.windows import Chunk

from .run_fakes import FakeAppsFlyer, make_loaded

_NOW = datetime(2026, 9, 22, 12, 0, 0)
_REPORT_TYPE = "geo_by_date_report"
_DAY = date(2026, 1, 1)


def _job(report_type: str = _REPORT_TYPE) -> ChunkJob:
    extract = make_loaded("standard", report_type=report_type)
    chunk = Chunk(index=0, from_date=_DAY, to_date=_DAY)
    return ChunkJob(
        extract=extract, app_id="app1", chunk=chunk, is_long=False, db="marts", table="t"
    )


def _process(
    job: ChunkJob, client: FakeAppsFlyer, *, defer_transient: bool, max_retries: int = 5
) -> tuple[object, list]:
    calls: list[tuple[ChunkJob, int, float]] = []

    def record_call(j: ChunkJob, api_calls: int, at: float) -> None:
        calls.append((j, api_calls, at))

    outcome = process_job(
        job,
        client=client,
        policy_factory=lambda: RetryPolicy(
            max_retries=max_retries, sleep=lambda s: None, defer_transient=defer_transient
        ),
        run_id="run1",
        now=lambda: _NOW,
        clock=lambda: 0.0,
        record_call=record_call,
        started_at=_NOW,
    )
    return outcome, calls


@pytest.mark.unit
def test_transient_deferred_signal_carries_api_calls_http_status_and_error() -> None:
    job = _job()
    client = FakeAppsFlyer()
    client.script(_REPORT_TYPE, "app1", _DAY, _DAY, (503, "server overloaded"))

    outcome, calls = _process(job, client, defer_transient=True)

    assert outcome.result is None  # non-terminal, same shape as a RateLimitError DEFERRED
    assert outcome.signal == TRANSIENT_DEFERRED
    assert outcome.api_calls == 1
    assert outcome.http_status == 503
    assert outcome.error is not None
    assert "status 503" in outcome.error
    assert "server overloaded" in outcome.error

    # record_call fired for the attempt: it was a real HTTP call and counts
    # against the key's spacing even though the chunk isn't terminal yet.
    assert len(calls) == 1
    assert calls[0][0] is job
    assert calls[0][1] == 1


@pytest.mark.unit
def test_transient_deferred_signal_is_distinct_from_rate_limit_deferred() -> None:
    assert TRANSIENT_DEFERRED != DEFERRED


@pytest.mark.unit
def test_transient_not_deferred_without_the_policy_flag_stays_a_terminal_failure() -> None:
    """`defer_transient=False` (afly apps/debug, and the default) keeps the
    pre-0.2.1 behaviour: RetryPolicy retries inline (fully, inside this one
    ``process_job`` call — not returned to the caller in between) and
    ``process_job``'s own ``TransientError`` branch still produces a terminal
    failed ``JobResult``, not a ``TRANSIENT_DEFERRED`` signal. ``max_retries=1``
    keeps this to a single HTTP attempt so the assertions stay simple."""
    job = _job()
    client = FakeAppsFlyer()
    client.script(_REPORT_TYPE, "app1", _DAY, _DAY, (503, "server overloaded"))

    outcome, calls = _process(job, client, defer_transient=False, max_retries=1)

    assert outcome.signal is None
    assert outcome.result is not None
    assert outcome.result.status == "failed"
    assert outcome.result.http_status == 503
    assert outcome.result.api_calls == 1
    assert len(calls) == 1
    assert calls[0][1] == 1
