"""Thin wrapper over ``clickhouse_driver.Client`` — the only place afly speaks
ClickHouse's native protocol.

Every other module in ``afly.database`` (and every future caller in M4) goes
through :class:`ClickHouseManager` rather than touching ``clickhouse_driver``
directly, for two reasons: driver exceptions get wrapped into
:class:`ClickHouseError` with the offending SQL attached (a bare
``clickhouse_driver.errors.ServerException`` printed to a CLI user is not
actionable — it doesn't say which statement failed), and every read method
normalizes ``DateTime64(..., 'UTC')`` columns back to naive UTC datetimes
(see :func:`_normalize_value`) so callers never have to think about it — the
project-wide contract (``afly.utils.datetime_utils``) is that every internal
timestamp is naive UTC, but the driver returns *aware* ``datetime`` objects
for any column with an explicit timezone, which every table this package
creates has.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import datetime
from typing import Any

from clickhouse_driver import Client

from afly.config.profile import ClickHouseProfile
from afly.database._http_client import HttpClient
from afly.utils.naming import fqn, quote_ident

_SQL_EXCERPT_LEN = 500
_DEFAULT_BATCH_SIZE = 50_000


class ClickHouseError(Exception):
    """Wraps a ``clickhouse_driver`` failure with the offending SQL attached.

    The SQL is truncated to :data:`_SQL_EXCERPT_LEN` chars — long enough to
    identify the failing statement, short enough that a batch INSERT's
    ``VALUES`` list (which never ends up in ``sql`` anyway — see
    :meth:`ClickHouseManager.insert_rows`) couldn't blow up a log line even
    if it did.
    """

    def __init__(self, message: str, *, sql: str | None = None) -> None:
        self.sql = sql
        message = _strip_server_stack_trace(message)
        if sql:
            excerpt = sql.strip()
            if len(excerpt) > _SQL_EXCERPT_LEN:
                excerpt = excerpt[:_SQL_EXCERPT_LEN] + " …"
            message = f"{message} — SQL: {excerpt}"
        super().__init__(message)


def _strip_server_stack_trace(message: str) -> str:
    """Drop the C++ stack trace ClickHouse appends to ``DB::Exception`` text.

    The first line (``Code: 81. DB::Exception: Database x doesn't exist``) is
    the whole diagnosis; the dozen ``@ 0x... in /usr/bin/clickhouse`` frames
    that follow are noise for a CLI user and bury the SQL excerpt we append.
    """
    head, sep, _tail = message.partition("Stack trace")
    return head.strip() if sep else message.strip()


def _normalize_value(value: Any) -> Any:
    """Strip tzinfo from an aware ``datetime`` — see the module docstring."""
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def _chunks(rows: Sequence[Mapping[str, Any]], size: int) -> Iterator[Sequence[Mapping[str, Any]]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


class ClickHouseManager:
    """Everything ``afly.database`` needs from a ClickHouse connection.

    Accepts any object exposing the ``clickhouse_driver.Client.execute``
    signature in ``__init__`` — production code always passes a real
    ``Client`` (via :meth:`from_profile`), tests may pass a fake.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def from_profile(cls, profile: ClickHouseProfile) -> ClickHouseManager:
        """Build a manager from a validated ``profiles.yml`` ClickHouse block.

        Picks the transport by ``profile.protocol``: native
        ``clickhouse_driver.Client`` (default, unchanged) or
        :class:`~afly.database._http_client.HttpClient` (``clickhouse_connect``
        underneath — see that module's docstring for why both exist). Either
        way the port used is ``profile.effective_port`` (native 9000/9440,
        http 8123/8443, unless ``port`` is set explicitly); native connects
        lazily (first I/O is the caller's first ``execute``),
        ``clickhouse_connect.get_client`` does not (see
        :meth:`HttpClient.connect`).

        The session deliberately does **not** select ``profile.database`` as
        its current database (either transport): on a fresh warehouse that
        database may not exist yet (afly creates it with ``CREATE DATABASE
        IF NOT EXISTS``), and ClickHouse refuses the very first query of a
        session whose current database is missing (``Code: 81``) — before
        any CREATE could run. Every statement afly issues is fully qualified
        (``db.table``), so the session default (``default``) is never relied
        upon.
        """
        if profile.protocol == "http":
            client: Any = HttpClient.connect(
                host=profile.host,
                port=profile.effective_port,
                user=profile.user,
                password=profile.password,
                secure=profile.secure,
                verify=profile.verify,
                settings=profile.settings,
                connect_timeout=profile.connect_timeout,
                send_receive_timeout=profile.send_receive_timeout,
            )
        else:
            client = Client(
                host=profile.host,
                port=profile.effective_port,
                user=profile.user,
                password=profile.password,
                secure=profile.secure,
                verify=profile.verify,
                settings=profile.settings,
                connect_timeout=profile.connect_timeout,
                send_receive_timeout=profile.send_receive_timeout,
            )
        return cls(client)

    def execute(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        *,
        settings: dict[str, Any] | None = None,
    ) -> list[tuple[Any, ...]]:
        """Run *sql*, substituting *params* driver-side (``%(name)s`` style).

        Never pass INSERT row data here — a list/tuple/generator ``params``
        flips the driver into insert mode, which is not this method's
        contract (see :meth:`insert_rows`). ``params`` here is always a dict
        of scalar/collection values for ``WHERE``/``VALUES``-less queries.
        """
        try:
            return self._client.execute(sql, params, settings=settings)
        except Exception as exc:  # clickhouse_driver raises its own hierarchy
            raise ClickHouseError(str(exc), sql=sql) from exc

    def query_dicts(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Like :meth:`execute`, but rows come back as ``{column: value}`` dicts."""
        try:
            rows, columns_with_types = self._client.execute(sql, params, with_column_types=True)
        except Exception as exc:
            raise ClickHouseError(str(exc), sql=sql) from exc
        names = [c[0] for c in columns_with_types]
        return [
            {name: _normalize_value(value) for name, value in zip(names, row, strict=True)}
            for row in rows
        ]

    def insert_rows(
        self,
        db: str,
        table: str,
        columns: Sequence[str],
        rows: Iterable[Mapping[str, Any]],
        *,
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> int:
        """Insert dict *rows* (each keyed by *columns*) in ``batch_size`` chunks.

        Returns the number of rows actually sent — 0 for an empty *rows*
        without issuing any query, which lets callers unconditionally call
        this after computing "fresh rows for the day" without a special case.
        """
        row_list = list(rows)
        if not row_list:
            return 0
        cols = list(columns)
        sql = f"INSERT INTO {fqn(db, table)} ({', '.join(quote_ident(c) for c in cols)}) VALUES"
        total = 0
        for batch in _chunks(row_list, batch_size):
            tuples = [tuple(row[c] for c in cols) for row in batch]
            try:
                self._client.execute(sql, tuples)
            except Exception as exc:
                raise ClickHouseError(
                    f"insert into {fqn(db, table)} failed: {exc}", sql=sql
                ) from exc
            total += len(tuples)
        return total

    def table_exists(self, db: str, table: str) -> bool:
        rows = self.execute(
            "SELECT 1 FROM system.tables WHERE database = %(db)s AND name = %(table)s",
            {"db": db, "table": table},
        )
        return bool(rows)

    def create_database(self, db: str) -> None:
        self.execute(f"CREATE DATABASE IF NOT EXISTS {quote_ident(db)}")

    def create_table_as(self, db: str, table: str, src_db: str, src_table: str) -> None:
        self.execute(f"CREATE TABLE IF NOT EXISTS {fqn(db, table)} AS {fqn(src_db, src_table)}")

    def drop_table(self, db: str, table: str) -> None:
        self.execute(f"DROP TABLE IF EXISTS {fqn(db, table)}")

    def truncate(self, db: str, table: str) -> None:
        self.execute(f"TRUNCATE TABLE IF EXISTS {fqn(db, table)}")

    def count(
        self, db: str, table: str, where: str = "", params: dict[str, Any] | None = None
    ) -> int:
        sql = f"SELECT count() FROM {fqn(db, table)}"
        if where:
            sql += f" WHERE {where}"
        rows = self.execute(sql, params)
        return int(rows[0][0]) if rows else 0

    def partition_exists(self, db: str, table: str, partition_id: str) -> bool:
        """Whether *table* has an active part for *partition_id* (``system.parts``).

        ``partition_id`` is the string ``system.parts`` itself stores — for
        every table this package creates (single-column integer partition
        keys, e.g. ``toYYYYMMDD(date)``) that's just the digits, exactly what
        :func:`afly.database.ddl.partition_id` produces.
        """
        rows = self.execute(
            "SELECT 1 FROM system.parts WHERE database = %(db)s AND table = %(table)s "
            "AND partition_id = %(partition_id)s AND active = 1 LIMIT 1",
            {"db": db, "table": table, "partition_id": partition_id},
        )
        return bool(rows)

    def server_version(self) -> str:
        rows = self.execute("SELECT version()")
        return str(rows[0][0]) if rows else ""

    def close(self) -> None:
        disconnect = getattr(self._client, "disconnect", None)
        if callable(disconnect):
            disconnect()
