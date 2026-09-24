"""Fetch-and-parse for one ChunkJob — the AppsFlyer half of the executor.

Split out of ``executor.py`` as a free function (not a method) so the
AppsFlyer error handling — which is the bulk of the branching — can be read
and tested on its own, independent of wave/scheduler bookkeeping.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from afly.appsflyer.client import AppsFlyerClient
from afly.appsflyer.errors import (
    AuthError,
    EmptyBodyError,
    PermanentError,
    RateLimitError,
    TransientError,
)
from afly.appsflyer.pull_api import PullRequestSpec, fetch_report
from afly.appsflyer.retry import RetryPolicy
from afly.csvmap.parser import ReportContext, parse_report
from afly.run._job_result import JobResult
from afly.run.planner import ChunkJob

AUTH_ABORT = "auth_abort"
DEFERRED = "deferred"


@dataclass
class FetchOutcome:
    """``process_job``'s return value.

    - **Success**: ``result`` is a terminal ``JobResult`` (``status="success"``),
      ``rows``/``unknown_headers`` are populated, ``signal`` is ``None``.
    - **Rate-limited** (policy built with ``defer_rate_limits=True``):
      ``result`` is ``None`` — this job isn't terminal, the scheduler decides
      what happens to it via ``QuotaScheduler.defer`` — ``signal`` is
      :data:`DEFERRED`, and ``api_calls``/``retry_after`` describe the one
      HTTP attempt just spent (the caller accumulates ``api_calls`` across
      every attempt of the same job; this call only reports its own).
    - **Failed** (auth/permanent/transient/parse error): ``result`` is a
      terminal ``JobResult`` (``status="failed"``), ``signal`` is
      :data:`AUTH_ABORT` for an ``AuthError`` (the whole run should stop),
      else ``None``.
    """

    result: JobResult | None
    rows: list[dict[str, Any]] = field(default_factory=list)
    unknown_headers: list[str] = field(default_factory=list)
    signal: str | None = None
    api_calls: int = 0
    retry_after: float | None = None


def process_job(
    job: ChunkJob,
    *,
    client: AppsFlyerClient,
    policy_factory: Callable[[], RetryPolicy],
    run_id: str,
    now: Callable[[], datetime],
    clock: Callable[[], float],
    record_call: Callable[[ChunkJob, int, float], None],
    started_at: datetime,
    currency: str | None = None,
) -> FetchOutcome:
    """Fetch + parse one job. See :class:`FetchOutcome` for the return shape.

    ``record_call`` is invoked on a rate-limited attempt too (not just on
    success) — AppsFlyer counted that HTTP call against the key's spacing and
    the account-wide ``min_gap_seconds`` either way, so the scheduler's
    bookkeeping has to see it or it would under-space the next attempt.

    ``currency`` is the app's own currency when the caller knows it (from
    the AppsFlyer app-list API) — stamped onto :class:`ReportContext` and
    used as a cross-check against the currency ``parse_report`` detects from
    the CSV's own ``Sales in <CUR>`` headers; see that dataclass's docstring
    for the precedence when they disagree.
    """
    policy = policy_factory()
    spec = PullRequestSpec.from_extract(job.extract.config)
    start_calls = client.api_calls

    try:
        raw = fetch_report(client, spec, job.app_id, job.chunk.from_date, job.chunk.to_date, policy)
    except AuthError as exc:
        result = JobResult(
            job=job,
            status="failed",
            error=str(exc)[:500],
            started_at=started_at,
            finished_at=now(),
        )
        return FetchOutcome(result, signal=AUTH_ABORT)
    except RateLimitError as exc:
        api_calls = client.api_calls - start_calls
        record_call(job, api_calls, clock())
        return FetchOutcome(None, signal=DEFERRED, api_calls=api_calls, retry_after=exc.retry_after)
    except (PermanentError, EmptyBodyError, TransientError) as exc:
        result = JobResult(
            job=job,
            status="failed",
            error=str(exc)[:500],
            started_at=started_at,
            finished_at=now(),
        )
        return FetchOutcome(result)

    record_call(job, raw.api_calls, clock())

    ctx = ReportContext(
        app_id=job.app_id,
        report_type=job.extract.config.report_type,
        category=job.extract.config.category,
        is_retargeting=job.extract.config.reattr,
        extract=job.extract.config.name,
        run_id=run_id,
        loaded_at=now(),
        currency=currency,
    )
    try:
        parsed = parse_report(
            raw.text,
            ctx,
            date_range=(job.chunk.from_date, job.chunk.to_date),
            exclude_media_sources=job.extract.config.exclude_media_sources,
            keep_unknown_columns=bool(job.extract.config.keep_unknown_columns),
        )
    except ValueError as exc:
        result = JobResult(
            job=job,
            status="failed",
            error=str(exc)[:500],
            api_calls=raw.api_calls,
            http_status=raw.status,
            started_at=started_at,
            finished_at=now(),
        )
        return FetchOutcome(result)

    result = JobResult(
        job=job,
        status="success",
        rows=len(parsed.rows),
        api_calls=raw.api_calls,
        http_status=raw.status,
        started_at=started_at,
        finished_at=now(),
    )
    return FetchOutcome(result, rows=parsed.rows, unknown_headers=parsed.unknown_headers)


__all__ = ["AUTH_ABORT", "DEFERRED", "FetchOutcome", "process_job"]
