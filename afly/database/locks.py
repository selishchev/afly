"""``_afly_locks`` repo: distributed run locks with stale-lock auto-recovery.

``ReplacingMergeTree(updated_at)`` keeps only the latest row per ``lock_key``
after a background merge — but merges aren't synchronous, so every read here
goes through ``FINAL`` rather than trusting merge timing. A lock is taken by
inserting a ``running`` row and released by inserting a ``released`` one on
top of it; there is no in-place update, matching the append-only style of
``_afly_loads``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from afly.database.clickhouse import ClickHouseManager
from afly.database.tables import LOCKS_COLUMN_NAMES
from afly.utils.datetime_utils import now_utc
from afly.utils.naming import fqn


class LockHeldError(Exception):
    """Raised when a lock is actively held by someone else (or was just lost to a race)."""

    def __init__(
        self, lock_key: str, owner: str, run_id: str, started_at: datetime, age_seconds: float
    ) -> None:
        super().__init__(
            f"lock {lock_key!r} is held by {owner!r} (run {run_id}, started {started_at.isoformat()}, "
            f"{age_seconds:.0f}s ago)"
        )
        self.lock_key = lock_key
        self.owner = owner
        self.run_id = run_id
        self.started_at = started_at
        self.age_seconds = age_seconds


class LocksRepo:
    def __init__(self, manager: ClickHouseManager, db: str, table: str) -> None:
        self._manager = manager
        self._db = db
        self._table = table

    @property
    def _fqn(self) -> str:
        return fqn(self._db, self._table)

    def _read_final(self, lock_key: str) -> dict[str, Any] | None:
        rows = self._manager.query_dicts(
            f"SELECT * FROM {self._fqn} FINAL WHERE lock_key = %(lock_key)s",
            {"lock_key": lock_key},
        )
        return rows[0] if rows else None

    def _write(
        self,
        lock_key: str,
        run_id: str,
        status: str,
        owner: str,
        started_at: datetime,
        timeout_seconds: int,
    ) -> None:
        self._manager.insert_rows(
            self._db,
            self._table,
            LOCKS_COLUMN_NAMES,
            [
                {
                    "lock_key": lock_key,
                    "run_id": run_id,
                    "status": status,
                    "owner": owner,
                    "started_at": started_at,
                    "updated_at": now_utc(),
                    "timeout_seconds": timeout_seconds,
                }
            ],
        )

    def acquire(
        self, lock_key: str, run_id: str, owner: str, timeout_seconds: int, *, force: bool = False
    ) -> None:
        """Take *lock_key*, or raise :class:`LockHeldError` if it's actively held.

        A ``running`` row older than its own ``timeout_seconds`` is stale —
        the owning process almost certainly died without releasing it — and
        is silently overridden rather than blocking forever. ``force=True``
        skips the held/stale check entirely (mirrors detectkit's
        ``acquire_lock``: the check isn't evaluated-and-ignored, it's not run
        at all). Either way, the write is followed by a confirming re-read:
        if a concurrent ``acquire`` won the race between our check and our
        insert, ``FINAL`` now shows *their* run_id, not ours, and that's
        reported the same way a pre-existing hold is.
        """
        if not force:
            existing = self._read_final(lock_key)
            if existing is not None and existing["status"] == "running":
                age = (now_utc() - existing["started_at"]).total_seconds()
                if age <= existing["timeout_seconds"]:
                    raise LockHeldError(
                        lock_key, existing["owner"], existing["run_id"], existing["started_at"], age
                    )
                # else: stale — fall through and take it.

        started_at = now_utc()
        self._write(lock_key, run_id, "running", owner, started_at, timeout_seconds)

        confirmed = self._read_final(lock_key)
        if confirmed is None:
            raise LockHeldError(lock_key, "?", "?", started_at, 0.0)
        if confirmed["run_id"] != run_id:
            age = (now_utc() - confirmed["started_at"]).total_seconds()
            raise LockHeldError(
                lock_key, confirmed["owner"], confirmed["run_id"], confirmed["started_at"], age
            )

    def release(self, lock_key: str, run_id: str) -> None:
        """Mark *lock_key* released, preserving the held row's owner/started_at/timeout."""
        existing = self._read_final(lock_key)
        owner = existing["owner"] if existing else ""
        started_at = existing["started_at"] if existing else now_utc()
        timeout_seconds = existing["timeout_seconds"] if existing else 0
        self._write(lock_key, run_id, "released", owner, started_at, timeout_seconds)

    def clear(self, lock_key: str) -> bool:
        """Force-release a (possibly stale, possibly not-yet-stale) lock.

        Used by ``afly unlock`` to recover from a hung run — the age check is
        deliberately not applied here, unlike :meth:`acquire`'s stale check.
        Returns ``True`` if a ``running`` row existed to clear.
        """
        existing = self._read_final(lock_key)
        if existing is None or existing["status"] != "running":
            return False
        self._write(
            lock_key,
            existing["run_id"],
            "released",
            existing["owner"],
            existing["started_at"],
            existing["timeout_seconds"],
        )
        return True

    def list_active(self) -> list[dict[str, Any]]:
        """Every currently ``running`` lock, with ``age_seconds``/``stale`` computed."""
        rows = self._manager.query_dicts(
            f"SELECT * FROM {self._fqn} FINAL WHERE status = 'running'"
        )
        now = now_utc()
        result = []
        for row in rows:
            age = (now - row["started_at"]).total_seconds()
            result.append({**row, "age_seconds": age, "stale": age > row["timeout_seconds"]})
        return result
