"""JobResult — the executor's in-flight, per-job bookkeeping record.

Split out of ``executor.py`` purely to keep that module under the house
line-length norm; nothing here is public API outside ``afly.run``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from afly.run.planner import ChunkJob


@dataclass
class JobResult:
    """The mutable outcome of one :class:`~afly.run.planner.ChunkJob`.

    Mutable (not frozen) on purpose: the empty-response guard and the
    whole-wave ClickHouse-failure path both need to downgrade an
    already-recorded ``"success"`` to ``"skipped"``/``"failed"`` in place,
    without losing the fields (``api_calls``, ``started_at``, ...) recorded
    at fetch time.
    """

    job: ChunkJob
    status: str  # "success" | "failed" | "skipped"
    rows: int = 0
    api_calls: int = 0
    http_status: int | None = None
    error: str | None = None
    skip_reason: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


def job_result_to_dict(result: JobResult) -> dict[str, Any]:
    """The ``summary.jobs[]`` entry shape for one :class:`JobResult`."""
    job = result.job
    return {
        "extract": job.extract.config.name,
        "app_id": job.app_id,
        "from": job.chunk.from_date,
        "to": job.chunk.to_date,
        "status": result.status,
        "rows": result.rows,
        "api_calls": result.api_calls,
        "http_status": result.http_status,
        "error": result.error,
        "skip_reason": result.skip_reason,
    }
