"""Structural types the Executor depends on from the ClickHouse layer.

Defined here (not imported from ``afly.database``) so the executor's own
type hints don't force that module to exist at import time — the two real
implementations (``LoadsRepo``/``PartitionRebuilder``) satisfy these
structurally, and so do the test fakes in ``tests/unit/run_fakes.py``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Protocol

from afly.schema import Granularity


class LoadsRepoLike(Protocol):
    """The slice of ``afly.database.loads.LoadsRepo`` the executor needs."""

    def start_chunk(self, load: Any, started_at: datetime) -> None: ...

    def finish_chunk(
        self,
        load: Any,
        status: str,
        *,
        started_at: datetime,
        finished_at: datetime,
        rows: int = 0,
        api_calls: int = 0,
        http_status: int | None = None,
        error: str | None = None,
        skip_reason: str | None = None,
    ) -> None: ...


class RebuilderLike(Protocol):
    """The slice of ``afly.database.writer.PartitionRebuilder`` the executor needs."""

    @property
    def granularity(self) -> Granularity: ...

    def prepare_staging(self) -> None: ...

    def drop_staging(self) -> None: ...

    def rows_for_pair(self, day: date, extract: str, app_id: str) -> int: ...

    def rebuild_partition(
        self, partition_id: str, coverage: set[tuple[date, str, str]], fresh_rows: list[Any]
    ) -> Any: ...


__all__ = ["LoadsRepoLike", "RebuilderLike"]
