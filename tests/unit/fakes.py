"""``FakeManager`` — a recording stand-in for ``ClickHouseManager`` in unit tests.

Implements the same method surface as ``afly.database.clickhouse.ClickHouseManager``
(duck-typed — Python doesn't check it's a subclass) so ``afly.database``'s
other modules (``ddl``, ``tables``, ``loads``, ``locks``, ``writer``) can be
unit-tested without Docker. It is a *spy with configurable canned responses*,
not a relational engine: every call is recorded (method name, positional
args, keyword args) in ``.calls`` for order/shape assertions, and the read
methods return whatever a test pre-loads onto the matching attribute/queue.
Genuine relational behaviour — real row filtering, real REPLACE/DROP
PARTITION semantics — is exercised against a live ClickHouse 22.11 server in
``tests/integration/test_clickhouse_layer.py``; this fake only needs to let
the *Python-side branching* (which statement runs when, what shows up in
``params``) be tested in isolation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any


class FakeManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

        # Convenience call logs, narrower than `.calls`, for the assertions
        # tests write most often.
        self.executed: list[tuple[str, dict[str, Any] | None]] = []
        self.query_dicts_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.inserted: list[tuple[str, str, tuple[str, ...], list[dict[str, Any]]]] = []

        # Configurable responses. `query_dicts_queue` is popped FIFO so a
        # test can script different answers to successive calls (e.g.
        # LocksRepo.acquire's pre-check read vs its post-insert confirm
        # read); once exhausted, `query_dicts_default` answers every further
        # call.
        self.query_dicts_queue: list[list[dict[str, Any]]] = []
        self.query_dicts_default: list[dict[str, Any]] = []
        self.execute_result: list[tuple[Any, ...]] = []
        self.table_exists_result: bool = False
        self.count_result: int = 0
        self.partition_exists_result: bool = False
        self.server_version_result: str = "22.11.0.0-fake"

        # Force a failure on the next `execute()` call, to exercise
        # `finally`-block cleanup paths. Raised once, then cleared.
        self.raise_on_execute: Exception | None = None

        self.closed = False

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def execute(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        *,
        settings: dict[str, Any] | None = None,
    ) -> list[tuple[Any, ...]]:
        self._record("execute", sql, params)
        self.executed.append((sql, params))
        if self.raise_on_execute is not None:
            exc, self.raise_on_execute = self.raise_on_execute, None
            raise exc
        return self.execute_result

    def query_dicts(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self._record("query_dicts", sql, params)
        self.query_dicts_calls.append((sql, params))
        if self.query_dicts_queue:
            return self.query_dicts_queue.pop(0)
        return self.query_dicts_default

    def insert_rows(
        self,
        db: str,
        table: str,
        columns: Sequence[str],
        rows: Iterable[Mapping[str, Any]],
        *,
        batch_size: int = 50_000,
    ) -> int:
        row_list = [dict(r) for r in rows]
        self._record("insert_rows", db, table, tuple(columns), row_list)
        self.inserted.append((db, table, tuple(columns), row_list))
        return len(row_list)

    def table_exists(self, db: str, table: str) -> bool:
        self._record("table_exists", db, table)
        return self.table_exists_result

    def create_database(self, db: str) -> None:
        self._record("create_database", db)

    def create_table_as(self, db: str, table: str, src_db: str, src_table: str) -> None:
        self._record("create_table_as", db, table, src_db, src_table)

    def drop_table(self, db: str, table: str) -> None:
        self._record("drop_table", db, table)

    def truncate(self, db: str, table: str) -> None:
        self._record("truncate", db, table)

    def count(
        self, db: str, table: str, where: str = "", params: dict[str, Any] | None = None
    ) -> int:
        self._record("count", db, table, where, params)
        return self.count_result

    def partition_exists(self, db: str, table: str, partition_id: str) -> bool:
        self._record("partition_exists", db, table, partition_id)
        return self.partition_exists_result

    def server_version(self) -> str:
        self._record("server_version")
        return self.server_version_result

    def close(self) -> None:
        self._record("close")
        self.closed = True

    # -- test-only convenience, not part of ClickHouseManager's interface --

    def call_names(self) -> list[str]:
        """The ordered list of method names called — the usual assertion target."""
        return [name for name, _, _ in self.calls]
