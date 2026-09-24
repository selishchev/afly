"""HTTP-transport adapter — the ``clickhouse_connect`` counterpart to the
native ``clickhouse_driver.Client`` this package speaks by default.

Why this exists: the analyst's machine can only reach the target ClickHouse
26.3 warehouse over HTTP (port 8423) — the native port accepts a TCP
connection but never answers the handshake — while the production worker may
still be native-only. :class:`ClickHouseProfile.protocol` (``afly.config.
profile``) picks between them; :meth:`ClickHouseManager.from_profile`
(``afly.database.clickhouse``) is the only place that reads it.

This module is deliberately an *adapter*, not a second implementation of
``ClickHouseManager``'s logic: :class:`HttpClient` exposes only the same
narrow slice of ``clickhouse_driver.Client`` that ``ClickHouseManager``
already calls (``execute``, ``disconnect``), so every other module in
``afly.database`` (``ddl``, ``tables``, ``loads``, ``locks``, ``writer``,
``checks``) works unmodified against either transport.

Two behavioural gaps between ``clickhouse_connect`` and ``clickhouse_driver``
had to be closed explicitly (found by comparing inserted/read-back rows on a
live server, not from documentation):

1. **Bulk INSERT is a different call, not an ``execute()`` mode.**
   ``clickhouse_driver.Client.execute(sql, rows)`` auto-detects "insert mode"
   when ``params`` is a list/tuple/generator (see its own docstring) and
   streams the rows as ``VALUES``. ``clickhouse_connect`` has no equivalent
   overload on ``.query()``/``.command()`` — bulk rows go through the
   dedicated ``.insert(table, data=..., column_names=...)``. ``execute()``
   below reproduces the native auto-detection (``isinstance(params, (list,
   tuple))``) and routes to ``.insert()``, parsing ``table``/``column_names``
   back out of the one INSERT shape :meth:`ClickHouseManager.insert_rows`
   ever generates (see :data:`_INSERT_SQL_RE`) — it is not a general SQL
   parser.
2. **A naive (tzinfo-less) ``datetime`` handed to ``.insert()`` is, by
   ``clickhouse_connect``'s own default, assumed to be in the *local
   machine's* timezone** and converted to UTC before the column write —
   verified against a live server: inserting ``datetime(2026, 9, 1, 10, 30)``
   from a UTC+3 machine landed as ``07:30`` in a ``DateTime64(3, 'UTC')``
   column. That is the opposite of ``clickhouse_driver`` (which writes a
   naive value as-is) and of this project's own naive-UTC contract
   (``afly.utils.datetime_utils``). The module-level
   ``common.set_setting("naive_datetime_insert", "server")`` below makes
   ``clickhouse_connect`` treat a naive value as already being in the
   column's own declared timezone instead — UTC, for every ``DateTime64``
   column this package writes — matching ``clickhouse_driver`` exactly. This
   setting is process-global in ``clickhouse_connect`` (not per-client); afly
   is a short-lived CLI invocation against one profile at a time, so that is
   fine to set unconditionally at import time.

Parameter substitution for ordinary (non-insert) queries needed no such
fix: ``clickhouse_connect.driver.binding.finalize_query`` renders a
``dict`` of scalar/collection params into ``%(name)s``-style SQL text the
same way ``clickhouse_driver`` does for every shape afly actually sends —
str, int, date, naive datetime (formatted as a literal wall-clock string,
*not* reinterpreted through a timezone — this is a different code path
from the ``.insert()`` column writer above), and a list of tuples of str
(for the ``(_extract, app_id) NOT IN %(pairs)s`` shape) — verified
byte-for-byte against ``clickhouse_driver``'s own renderer; see
``tests/unit/test_http_client.py``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import clickhouse_connect
from clickhouse_connect import common
from clickhouse_connect.driver.client import Client as ConnectClient

# See point 2 in the module docstring. Set once, at import time, for the
# whole process.
common.set_setting("naive_datetime_insert", "server")

# The exact (and only) shape ClickHouseManager.insert_rows builds:
#   INSERT INTO `db`.`table` (`col1`, `col2`, ...) VALUES
# `\S+` for the target relies on afly's own fqn()/quote_ident() never
# introducing whitespace; `[^)]+` for the column list relies on afly's own
# column names never containing `)` — both true for every table this
# package creates (destination, `_afly_loads`, `_afly_locks`). This is not a
# general INSERT-statement parser and isn't meant to be one.
_INSERT_SQL_RE = re.compile(r"^INSERT INTO (?P<target>\S+) \((?P<cols>[^)]+)\) VALUES$")


def _unquote_ident(quoted: str) -> str:
    """Reverse ``afly.utils.naming.quote_ident``: strip backticks, un-escape doubled ones."""
    ident = quoted.strip()
    if ident.startswith("`") and ident.endswith("`"):
        ident = ident[1:-1]
    return ident.replace("``", "`")


class HttpClient:
    """Adapts ``clickhouse_connect``'s HTTP client to the slice of ``clickhouse_driver.Client`` ``ClickHouseManager`` uses.

    Constructed via :meth:`connect` (mirrors ``clickhouse_driver.Client``'s
    lazy-connect posture as closely as clickhouse_connect allows — see
    :meth:`connect`'s docstring for the one difference).
    """

    def __init__(self, client: ConnectClient) -> None:
        self._client = client

    @classmethod
    def connect(
        cls,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        secure: bool,
        verify: bool,
        settings: dict[str, Any],
        connect_timeout: int,
        send_receive_timeout: int,
    ) -> HttpClient:
        """Build an ``HttpClient`` from the same fields ``ClickHouseManager.from_profile`` passes for native.

        Unlike ``clickhouse_driver.Client(...)`` (construction never touches
        the network), ``clickhouse_connect.get_client(...)`` performs a
        lightweight handshake (a ``SELECT 1``-equivalent server ping)
        immediately — so a bad host/port can surface here rather than on the
        first ``execute()``. Deliberately no ``database=`` — see the module
        docstring on ``ClickHouseManager.from_profile`` for why afly never
        selects a session database.
        """
        client = clickhouse_connect.get_client(
            host=host,
            port=port,
            username=user,
            password=password,
            secure=secure,
            verify=verify,
            settings=settings,
            connect_timeout=connect_timeout,
            send_receive_timeout=send_receive_timeout,
        )
        return cls(client)

    def execute(
        self,
        sql: str,
        params: dict[str, Any] | Sequence[Any] | None = None,
        *,
        settings: dict[str, Any] | None = None,
        with_column_types: bool = False,
    ) -> list[tuple[Any, ...]] | tuple[list[tuple[Any, ...]], list[tuple[str, str]]]:
        """Mirrors ``clickhouse_driver.Client.execute``'s calling convention — see the module docstring, point 1.

        ``params`` is a dict of scalar/collection values for an ordinary
        query (rendered into ``%(name)s`` placeholders), or a list/tuple of
        row-tuples for the bulk-INSERT case — the same discriminator
        ``clickhouse_driver`` itself uses
        (``isinstance(params, (list, tuple, ...))``).
        """
        if isinstance(params, (list, tuple)):
            self._insert(sql, params, settings=settings)
            return ([], []) if with_column_types else []

        result = self._client.query(sql, parameters=params, settings=settings)
        rows = [tuple(row) for row in result.result_set]
        if with_column_types:
            columns = [
                (name, ch_type.name)
                for name, ch_type in zip(result.column_names, result.column_types, strict=True)
            ]
            return rows, columns
        return rows

    def _insert(self, sql: str, rows: Sequence[Any], *, settings: dict[str, Any] | None) -> None:
        match = _INSERT_SQL_RE.match(sql.strip())
        if match is None:
            raise ValueError(
                "HttpClient can only bulk-insert via ClickHouseManager.insert_rows's own "
                f"'INSERT INTO ... (...) VALUES' shape — got: {sql!r}"
            )
        target = match["target"]
        column_names = [_unquote_ident(c) for c in match["cols"].split(", ")]
        # ClickHouseManager.insert_rows returns len(tuples) itself (computed
        # before this call) rather than trusting the client's own count, so
        # .insert()'s return value (a QuerySummary) is intentionally discarded.
        self._client.insert(target, data=list(rows), column_names=column_names, settings=settings)

    def disconnect(self) -> None:
        self._client.close()
