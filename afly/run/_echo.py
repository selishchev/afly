"""Live per-job output lines for the Executor — split out to keep executor.py
under the house line-length norm.
"""

from __future__ import annotations

from collections.abc import Callable

from afly.cli._output import echo_error, echo_warning
from afly.run.planner import ChunkJob


def echo_success(echo: Callable[[str], None], job: ChunkJob, rows: int, api_calls: int) -> None:
    echo(
        f"  • {job.extract.config.name} {job.app_id} "
        f"{job.chunk.from_date}..{job.chunk.to_date}: {rows} rows, {api_calls} calls"
    )


def echo_fail(job: ChunkJob, message: str) -> None:
    echo_error(
        f"{job.chunk.from_date}..{job.chunk.to_date}: {message}",
        name=f"{job.extract.config.name} {job.app_id}",
    )


def echo_skip(job: ChunkJob, reason: str) -> None:
    echo_warning(
        f"{job.chunk.from_date}..{job.chunk.to_date}: {reason}",
        name=f"{job.extract.config.name} {job.app_id}",
    )


__all__ = ["echo_success", "echo_fail", "echo_skip"]
