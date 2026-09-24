"""Per-run table setup: acquire locks, then prep a destination/rebuilder per table.

Bundled as a context manager so a failure partway through (a lock already
held on table 2 of 3, a schema mismatch on the destination, ...) always
releases whatever was already acquired/prepared — never leaves a lock or a
staging table behind just because a later table in the plan failed.

Deliberately knows nothing about ``afly.database`` itself — *how* a
destination/rebuilder gets prepared is the caller's ``prepare_fn``
(``afly.run.runner`` wires the real ``ensure_destination`` +
``PartitionRebuilder``; tests wire ``FakeRebuilder`` directly). That keeps
this module — and its cleanup-on-failure guarantee — testable without a
ClickHouse layer at all.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Callable
from typing import Any

from afly.cli._output import echo_warning


def owner_string() -> str:
    """``hostname:pid`` — the lock ``owner`` value every afly process uses."""
    return f"{socket.gethostname()}:{os.getpid()}"


class TableSetup:
    def __init__(
        self,
        locks: Any,
        run_id: str,
        owner: str,
        timeout_seconds: int,
        *,
        force: bool = False,
        prepare_fn: Callable[[str, str], Any],
    ) -> None:
        self._locks = locks
        self._run_id = run_id
        self._owner = owner
        self._timeout_seconds = timeout_seconds
        self._force = force
        self._prepare_fn = prepare_fn
        self._acquired: list[str] = []
        self.rebuilders: dict[tuple[str, str], Any] = {}

    def prepare(self, db: str, table: str) -> None:
        """Lock *db.table*, then run ``prepare_fn(db, table)`` to get its rebuilder.

        Raises whatever ``locks.acquire``/``prepare_fn`` raise (a real run:
        ``LockHeldError``/``SchemaMismatchError``) — the caller catches
        those; ``__exit__`` still cleans up whatever was already done.
        """
        lock_key = f"table:{db}.{table}"
        self._locks.acquire(
            lock_key, self._run_id, self._owner, self._timeout_seconds, force=self._force
        )
        self._acquired.append(lock_key)

        self.rebuilders[(db, table)] = self._prepare_fn(db, table)

    def __enter__(self) -> TableSetup:
        return self

    def __exit__(self, *exc_info: object) -> None:
        for lock_key in self._acquired:
            try:
                self._locks.release(lock_key, self._run_id)
            except Exception as exc:  # noqa: BLE001 -- never let cleanup mask the real failure
                echo_warning(f"failed to release lock {lock_key}: {exc}")
        for rebuilder in self.rebuilders.values():
            try:
                rebuilder.drop_staging()
            except Exception as exc:  # noqa: BLE001
                echo_warning(f"failed to drop staging table: {exc}")


__all__ = ["TableSetup", "owner_string"]
