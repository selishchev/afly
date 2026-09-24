"""``_afly_loads`` repo: the append-only idempotency ledger + quota bookkeeping.

Every chunk-load attempt writes two rows (start, then finish) rather than
updating one — ClickHouse has no cheap in-place update, and an append-only
ledger is also just a better audit log: a chunk that started but never
finished (crash mid-pull) leaves a visible ``running`` row forever instead of
silently vanishing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from afly import __version__
from afly.database.clickhouse import ClickHouseManager
from afly.database.tables import LOADS_COLUMN_NAMES
from afly.utils.naming import fqn

_EPOCH = date(1970, 1, 1)
_VALID_STATUSES = ("success", "failed", "skipped")


@dataclass(frozen=True)
class ChunkLoad:
    """Identifies one AppsFlyer pull chunk — the immutable half of a loads row.

    Shared by :meth:`LoadsRepo.start_chunk` and :meth:`LoadsRepo.finish_chunk`
    so the two rows for one attempt always agree on what they're describing.
    """

    run_id: str
    extract: str
    app_id: str
    report_type: str
    from_date: date
    to_date: date
    chunk_days: int
    is_long: bool


class LoadsRepo:
    def __init__(self, manager: ClickHouseManager, db: str, table: str) -> None:
        self._manager = manager
        self._db = db
        self._table = table

    @property
    def _fqn(self) -> str:
        return fqn(self._db, self._table)

    def _insert(self, row: dict[str, object]) -> None:
        self._manager.insert_rows(self._db, self._table, LOADS_COLUMN_NAMES, [row])

    def start_chunk(self, load: ChunkLoad, started_at: datetime) -> None:
        """Record a chunk as ``running`` the moment a pull begins."""
        self._insert(
            {
                "run_id": load.run_id,
                "extract": load.extract,
                "app_id": load.app_id,
                "report_type": load.report_type,
                "from_date": load.from_date,
                "to_date": load.to_date,
                "chunk_days": load.chunk_days,
                "is_long": 1 if load.is_long else 0,
                "status": "running",
                "rows": 0,
                "api_calls": 0,
                "http_status": None,
                "started_at": started_at,
                "finished_at": None,
                "duration_ms": None,
                "error": None,
                "skip_reason": None,
                "afly_version": __version__,
            }
        )

    def finish_chunk(
        self,
        load: ChunkLoad,
        status: str,
        *,
        started_at: datetime,
        finished_at: datetime,
        rows: int = 0,
        api_calls: int = 0,
        http_status: int | None = None,
        error: str | None = None,
        skip_reason: str | None = None,
    ) -> None:
        """Record the outcome of a chunk — a second, independent row (no mutation)."""
        if status not in _VALID_STATUSES:
            raise ValueError(f"invalid status {status!r} — expected one of {_VALID_STATUSES}")
        duration_ms = max(int((finished_at - started_at).total_seconds() * 1000), 0)
        self._insert(
            {
                "run_id": load.run_id,
                "extract": load.extract,
                "app_id": load.app_id,
                "report_type": load.report_type,
                "from_date": load.from_date,
                "to_date": load.to_date,
                "chunk_days": load.chunk_days,
                "is_long": 1 if load.is_long else 0,
                "status": status,
                "rows": rows,
                "api_calls": api_calls,
                "http_status": http_status,
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_ms": duration_ms,
                "error": error,
                "skip_reason": skip_reason,
                "afly_version": __version__,
            }
        )

    def watermark(self, extract: str, app_id: str) -> date | None:
        """The latest ``to_date`` successfully loaded for (*extract*, *app_id*).

        ``max()`` over an empty ClickHouse result set returns the column's
        default (``1970-01-01`` for ``Date``), not ``NULL`` — that sentinel is
        translated to ``None`` here so callers never have to know about it.
        """
        rows = self._manager.query_dicts(
            f"SELECT max(to_date) AS wm FROM {self._fqn} "
            "WHERE extract = %(extract)s AND app_id = %(app_id)s AND status = 'success'",
            {"extract": extract, "app_id": app_id},
        )
        wm = rows[0]["wm"] if rows else None
        if wm is None or wm == _EPOCH:
            return None
        return wm

    def long_calls_today(self, today: date) -> tuple[int, dict[str, int]]:
        """Long-call quota already spent today: ``(account_total, {app_id: count})``.

        Excludes ``running`` rows — a chunk still in flight hasn't actually
        drawn down the quota yet (and if it dies mid-flight without a
        ``finish_chunk``, it must not silently eat quota forever).
        """
        start = datetime(today.year, today.month, today.day)
        rows = self._manager.query_dicts(
            f"SELECT app_id, sum(api_calls) AS calls FROM {self._fqn} "
            "WHERE is_long = 1 AND status != 'running' AND started_at >= %(start)s "
            "GROUP BY app_id",
            {"start": start},
        )
        per_app = {r["app_id"]: int(r["calls"]) for r in rows}
        return sum(per_app.values()), per_app

    def recent(self, limit: int = 50) -> list[dict[str, object]]:
        """The most recent loads rows, newest first — for a future ``afly status``."""
        return self._manager.query_dicts(
            f"SELECT * FROM {self._fqn} ORDER BY started_at DESC LIMIT %(limit)s",
            {"limit": limit},
        )
