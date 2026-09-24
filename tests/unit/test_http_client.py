"""Tests for `afly.database._http_client.HttpClient` — the clickhouse_connect adapter.

Uses a `FakeConnectClient` (a recording spy shaped like
`clickhouse_connect.driver.client.Client`, not a real one) for the
call-shape assertions — genuine wire behaviour against a real server is
covered by the protocol-parametrized `tests/integration/test_clickhouse_layer.py`.
Parameter-rendering equivalence with `clickhouse_driver` is checked here
against clickhouse_connect's *real* binding module (no fake), since that's
exactly the code path a fake would paper over.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pytest

from afly.config.profile import ClickHouseProfile
from afly.database import _http_client as http_client_module
from afly.database._http_client import HttpClient
from afly.database.clickhouse import ClickHouseError, ClickHouseManager


@dataclass
class _FakeColumnType:
    name: str


@dataclass
class _FakeQueryResult:
    result_set: list[tuple[Any, ...]]
    column_names: tuple[str, ...] = ()
    column_types: tuple[_FakeColumnType, ...] = ()


@dataclass
class FakeConnectClient:
    """Records `.query`/`.insert`/`.close` calls; shaped like `clickhouse_connect`'s `Client`."""

    query_result: _FakeQueryResult = field(default_factory=lambda: _FakeQueryResult(result_set=[]))
    query_calls: list[tuple[str, Any, Any]] = field(default_factory=list)
    insert_calls: list[dict[str, Any]] = field(default_factory=list)
    closed: bool = False
    raise_on_query: Exception | None = None

    def query(self, sql, parameters=None, settings=None):  # noqa: ANN001
        self.query_calls.append((sql, parameters, settings))
        if self.raise_on_query is not None:
            raise self.raise_on_query
        return self.query_result

    def insert(self, table, data=None, column_names=None, settings=None):  # noqa: ANN001
        self.insert_calls.append(
            {"table": table, "data": data, "column_names": column_names, "settings": settings}
        )

    def close(self) -> None:
        self.closed = True


# ── naive_datetime_insert is set to "server" at import time ────────────────


@pytest.mark.unit
def test_module_sets_naive_datetime_insert_to_server() -> None:
    """See the module docstring, point 2 — a naive datetime must bind as UTC, not local time."""
    from clickhouse_connect import common

    assert common.get_setting("naive_datetime_insert") == "server"


# ── execute(): ordinary query delegates params/settings through unchanged ──


@pytest.mark.unit
def test_execute_delegates_dict_params_and_settings_to_query() -> None:
    fake = FakeConnectClient(query_result=_FakeQueryResult(result_set=[(1, "a"), (2, "b")]))
    client = HttpClient(fake)

    rows = client.execute(
        "SELECT a, b FROM t WHERE x = %(x)s", {"x": 5}, settings={"max_threads": 1}
    )

    assert rows == [(1, "a"), (2, "b")]
    assert fake.query_calls == [
        ("SELECT a, b FROM t WHERE x = %(x)s", {"x": 5}, {"max_threads": 1})
    ]


@pytest.mark.unit
def test_execute_with_no_params_passes_none_through() -> None:
    fake = FakeConnectClient(query_result=_FakeQueryResult(result_set=[(1,)]))
    client = HttpClient(fake)

    client.execute("SELECT 1")

    assert fake.query_calls == [("SELECT 1", None, None)]


# ── execute(with_column_types=True): shape matches clickhouse_driver's ─────


@pytest.mark.unit
def test_execute_with_column_types_returns_rows_and_name_type_pairs() -> None:
    fake = FakeConnectClient(
        query_result=_FakeQueryResult(
            result_set=[("x", 1)],
            column_names=("name", "n"),
            column_types=(_FakeColumnType("String"), _FakeColumnType("UInt64")),
        )
    )
    client = HttpClient(fake)

    rows, columns = client.execute("SELECT name, n FROM t", with_column_types=True)

    assert rows == [("x", 1)]
    assert columns == [("name", "String"), ("n", "UInt64")]


@pytest.mark.unit
def test_execute_rows_are_tuples_even_if_the_client_returns_lists() -> None:
    """DDL/administrative statements come back from clickhouse_connect as lists, not tuples."""
    fake = FakeConnectClient(query_result=_FakeQueryResult(result_set=[[0, 0, 0]]))
    client = HttpClient(fake)

    rows = client.execute("CREATE DATABASE IF NOT EXISTS x")

    assert rows == [(0, 0, 0)]
    assert all(isinstance(r, tuple) for r in rows)


# ── execute(): bulk-INSERT auto-detection (list/tuple params -> .insert()) ─


@pytest.mark.unit
def test_execute_with_list_of_tuples_routes_to_insert_with_parsed_table_and_columns() -> None:
    fake = FakeConnectClient()
    client = HttpClient(fake)
    sql = "INSERT INTO `marts`.`af_reports` (`app_id`, `event_counter`, `roi`) VALUES"
    rows = [
        ("app1", {"install": 3}, None),
        ("app2", {}, 1.5),
    ]

    result = client.execute(sql, rows)

    assert result == []
    assert fake.query_calls == []  # never went through .query()
    assert len(fake.insert_calls) == 1
    call = fake.insert_calls[0]
    assert call["table"] == "`marts`.`af_reports`"
    assert call["column_names"] == ["app_id", "event_counter", "roi"]
    # Map values and None (Nullable) pass through untouched — no reshaping.
    assert call["data"] == rows
    assert call["data"][0][1] == {"install": 3}
    assert call["data"][0][2] is None


@pytest.mark.unit
def test_execute_insert_preserves_row_and_column_order() -> None:
    fake = FakeConnectClient()
    client = HttpClient(fake)
    sql = "INSERT INTO `db`.`t` (`c1`, `c2`, `c3`) VALUES"
    rows = [(1, 2, 3), (4, 5, 6)]

    client.execute(sql, rows)

    call = fake.insert_calls[0]
    assert call["column_names"] == ["c1", "c2", "c3"]
    assert call["data"] == [(1, 2, 3), (4, 5, 6)]


@pytest.mark.unit
def test_execute_insert_with_tuple_rows_also_works() -> None:
    """`params` may be a tuple of rows, not just a list — same as clickhouse_driver accepts."""
    fake = FakeConnectClient()
    client = HttpClient(fake)

    client.execute(
        "INSERT INTO `db`.`t` (`a`) VALUES", (("x",), ("y",))  # tuple-of-tuples, not a list
    )

    assert fake.insert_calls[0]["data"] == [("x",), ("y",)]


@pytest.mark.unit
def test_execute_insert_rejects_unrecognized_sql_shape() -> None:
    fake = FakeConnectClient()
    client = HttpClient(fake)

    with pytest.raises(ValueError, match="INSERT INTO"):
        client.execute("INSERT INTO t VALUES", [(1,)])  # no column list — not afly's own shape


# ── error propagation (ClickHouseManager does the wrapping) ────────────────


@pytest.mark.unit
def test_query_error_propagates_unwrapped_from_http_client() -> None:
    """HttpClient itself does not wrap errors — ClickHouseManager's generic `except Exception` does."""
    fake = FakeConnectClient(raise_on_query=RuntimeError("boom"))
    client = HttpClient(fake)

    with pytest.raises(RuntimeError, match="boom"):
        client.execute("SELECT 1")


@pytest.mark.unit
def test_clickhouse_manager_wraps_http_client_errors_into_clickhouse_error() -> None:
    fake = FakeConnectClient(raise_on_query=RuntimeError("server exploded\nStack trace:\n  at foo"))
    manager = ClickHouseManager(HttpClient(fake))

    with pytest.raises(ClickHouseError) as excinfo:
        manager.execute("SELECT 1")

    message = str(excinfo.value)
    assert "server exploded" in message
    assert "Stack trace" not in message  # _strip_server_stack_trace applies uniformly
    assert "SELECT 1" in message  # SQL excerpt attached, same as the native path


@pytest.mark.unit
def test_clickhouse_manager_wraps_http_client_insert_errors() -> None:
    class _RaisingInsertClient(FakeConnectClient):
        def insert(self, table, data=None, column_names=None, settings=None):  # noqa: ANN001
            raise RuntimeError("insert failed")

    manager = ClickHouseManager(HttpClient(_RaisingInsertClient()))

    with pytest.raises(ClickHouseError, match="insert into"):
        manager.insert_rows("db", "t", ["a"], [{"a": 1}])


# ── disconnect() ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_disconnect_closes_the_underlying_client() -> None:
    fake = FakeConnectClient()
    client = HttpClient(fake)

    client.disconnect()

    assert fake.closed is True


# ── parameter rendering: clickhouse_connect vs clickhouse_driver, for the shapes afly uses ──


@pytest.mark.unit
def test_param_rendering_matches_clickhouse_driver_for_afly_shapes() -> None:
    """Compares the two drivers' own renderers directly — no adapter code runs either.

    This is the check called for by the HTTP-transport task: if
    clickhouse_connect ever renders one of afly's param shapes differently
    from clickhouse_driver, this test catches it (and the fix belongs in
    `_http_client.py`, converting the value before it reaches
    clickhouse_connect — see the module docstring). As of clickhouse-connect
    1.9.0 every shape below matches byte-for-byte.
    """
    clickhouse_driver = pytest.importorskip("clickhouse_driver")
    from clickhouse_connect.driver.binding import finalize_query
    from clickhouse_driver.context import Context
    from clickhouse_driver.util.escape import escape_params

    cases: dict[str, Any] = {
        "s": "O'Brien",
        "n": 42,
        "d": date(2026, 9, 1),
        "dt": datetime(2026, 9, 23, 10, 30, 0),  # naive — query-param path, not the insert path
        "pairs": [("facebook", "app1"), ("standard", "app2")],
    }
    query = (
        "WHERE s = %(s)s AND n = %(n)s AND d = %(d)s AND dt = %(dt)s AND (x, y) NOT IN %(pairs)s"
    )

    ctx = Context()
    # escape_datetime() reads context.server_info.get_timezone() even for a
    # naive value (its tzinfo-is-None branch never uses the result) — a real
    # Client sets this from the connection handshake; afly's own tables are
    # always UTC, so that's what a real session would report here too.
    ctx.server_info = type("_FakeServerInfo", (), {"get_timezone": lambda self: "UTC"})()
    driver_rendered = query % escape_params(cases, ctx)
    connect_rendered = finalize_query(query, cases)

    assert connect_rendered == driver_rendered
    assert str(clickhouse_driver.__name__) == "clickhouse_driver"  # keep the import "used"


@pytest.mark.unit
def test_param_rendering_matches_for_writer_triples_shape_with_a_date_element() -> None:
    """`PartitionRebuilder.rebuild_partition`'s `(date, _extract, app_id) NOT
    IN %(triples)s` shape (afly.database.writer) — a `Date` element inside a
    tuple inside a list, which afly's own coverage sets always have as the
    first element. Distinct from the all-string `pairs` shape checked above:
    this is exactly the requirement that configurable partition granularity
    added (coverage went from `(extract, app_id)` to `(day, extract,
    app_id)`), so it gets its own byte-for-byte comparison rather than
    trusting the string-tuple case to generalize.
    """
    pytest.importorskip("clickhouse_driver")
    from clickhouse_connect.driver.binding import finalize_query
    from clickhouse_driver.context import Context
    from clickhouse_driver.util.escape import escape_params

    triples = [
        (date(2026, 9, 10), "facebook", "app1"),
        (date(2026, 9, 30), "standard", "app2"),
    ]
    cases: dict[str, Any] = {"pid": 202609, "triples": sorted(triples)}
    query = "toYYYYMM(date) = %(pid)s AND (date, _extract, app_id) NOT IN %(triples)s"

    ctx = Context()
    ctx.server_info = type("_FakeServerInfo", (), {"get_timezone": lambda self: "UTC"})()
    driver_rendered = query % escape_params(cases, ctx)
    connect_rendered = finalize_query(query, cases)

    assert connect_rendered == driver_rendered
    assert "2026-09-10" in driver_rendered  # sanity: the Date actually rendered as a date literal


@pytest.mark.unit
def test_execute_inserts_two_identical_rows_both_via_http_transport() -> None:
    """Regression for the no-dedup product requirement (MergeTree, no
    ReplacingMergeTree collapse): two fully identical fresh rows must both
    reach `.insert()` — the HTTP adapter must not coalesce them."""
    fake = FakeConnectClient()
    client = HttpClient(fake)
    sql = "INSERT INTO `marts`.`af_reports` (`app_id`, `installs`) VALUES"
    identical_row = ("app1", 5)

    client.execute(sql, [identical_row, identical_row])

    assert fake.insert_calls[0]["data"] == [identical_row, identical_row]
    assert len(fake.insert_calls[0]["data"]) == 2


# ── from_profile: protocol selection + effective ports ─────────────────────


@pytest.mark.unit
def test_from_profile_native_builds_native_client_on_effective_port(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: dict[str, Any] = {}

    class _FakeNativeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("afly.database.clickhouse.Client", _FakeNativeClient)

    profile = ClickHouseProfile(host="ch.internal", database="marts", protocol="native")
    manager = ClickHouseManager.from_profile(profile)

    assert isinstance(manager._client, _FakeNativeClient)  # noqa: SLF001
    assert captured["port"] == 9000


@pytest.mark.unit
def test_from_profile_http_builds_http_client_on_effective_port(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: dict[str, Any] = {}

    def _fake_connect(**kwargs: Any) -> FakeConnectClient:
        captured.update(kwargs)
        return FakeConnectClient()

    monkeypatch.setattr(http_client_module.clickhouse_connect, "get_client", _fake_connect)

    profile = ClickHouseProfile(host="ch.internal", database="marts", protocol="http")
    manager = ClickHouseManager.from_profile(profile)

    assert isinstance(manager._client, HttpClient)  # noqa: SLF001
    assert captured["port"] == 8123
    assert captured["host"] == "ch.internal"


@pytest.mark.unit
def test_from_profile_http_secure_defaults_to_8443(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: dict[str, Any] = {}

    def _fake_connect(**kwargs: Any) -> FakeConnectClient:
        captured.update(kwargs)
        return FakeConnectClient()

    monkeypatch.setattr(http_client_module.clickhouse_connect, "get_client", _fake_connect)

    profile = ClickHouseProfile(host="ch.internal", database="marts", protocol="http", secure=True)
    ClickHouseManager.from_profile(profile)

    assert captured["port"] == 8443


@pytest.mark.unit
def test_from_profile_explicit_port_overrides_the_protocol_default(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: dict[str, Any] = {}

    def _fake_connect(**kwargs: Any) -> FakeConnectClient:
        captured.update(kwargs)
        return FakeConnectClient()

    monkeypatch.setattr(http_client_module.clickhouse_connect, "get_client", _fake_connect)

    profile = ClickHouseProfile(host="ch.internal", database="marts", protocol="http", port=8888)
    ClickHouseManager.from_profile(profile)

    assert captured["port"] == 8888
