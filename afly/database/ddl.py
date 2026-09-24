"""DDL for the destination MergeTree, and schema-drift detection against it.

``afly.schema.DESTINATION_COLUMNS`` is the single source of truth for both
the CSV parser (M2) and this module — a destination table this module
creates is guaranteed to match what the parser produces. What it can't
guarantee is a table the *user* created by hand, or one the schema grew new
columns for after it was created — that's what :func:`check_schema` /
:func:`ensure_destination` are for.

``granularity`` (``afly.schema.Granularity``: ``"month"`` default, or
``"day"``) controls the destination's ``PARTITION BY`` expression — see
``afly.schema.partition_by``. This is a **pre-release, no-migration**
project: there is no ``ALTER TABLE ... MODIFY PARTITION`` here or anywhere
else in this package, because ClickHouse doesn't offer one (re-partitioning
means rebuilding the table from scratch) — an existing table whose partition
key doesn't match the configured granularity is a hard :class:`SchemaMismatchError`,
not something afly tries to fix automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from afly.database.clickhouse import ClickHouseManager
from afly.schema import DESTINATION_COLUMNS, ORDER_BY, Granularity, partition_by
from afly.utils.naming import fqn, quote_ident


def destination_ddl(db: str, table: str, granularity: Granularity = "month") -> str:
    """``CREATE TABLE IF NOT EXISTS`` for the destination, from ``afly.schema``."""
    columns_sql = ",\n    ".join(
        f"{quote_ident(name)} {ch_type}" for name, ch_type in DESTINATION_COLUMNS
    )
    order_by_sql = ", ".join(quote_ident(c) for c in ORDER_BY)
    return (
        f"CREATE TABLE IF NOT EXISTS {fqn(db, table)} (\n"
        f"    {columns_sql}\n"
        f")\n"
        f"ENGINE = MergeTree\n"
        f"PARTITION BY {partition_by(granularity)}\n"
        f"ORDER BY ({order_by_sql})"
    )


def staging_table_name(table: str) -> str:
    """The per-destination staging table name a :class:`PartitionRebuilder` writes to."""
    return f"{table}__afly_staging"


def partition_id(day: date, granularity: Granularity = "month") -> str:
    """The ``system.parts.partition_id`` value for *day* under *granularity*'s ``PARTITION BY``.

    For a single-column integer partition key, ClickHouse's partition id is
    just the value's string form — no hashing (verified against a live
    22.11 server; also what ``ALTER TABLE ... PARTITION ID`` expects):
    ``"202609"`` for ``toYYYYMM(date)`` (month, the default), ``"20260910"``
    for ``toYYYYMMDD(date)`` (day).
    """
    return day.strftime("%Y%m") if granularity == "month" else day.strftime("%Y%m%d")


def _normalize_text(text: str) -> str:
    """Whitespace-insensitive comparison key for a ClickHouse type/expression string.

    ``system.columns.type``/``system.tables.partition_key`` have been
    observed to echo text back exactly (``"DateTime64(3, 'UTC')"``, space
    included) on 22.11, but nothing guarantees that's stable across
    versions — comparing with whitespace stripped is cheap insurance against
    a purely cosmetic mismatch producing a false "changed" report.
    """
    return "".join(text.split())


@dataclass
class SchemaCheck:
    """Result of comparing a live table's columns/partition key against expectations.

    Extra columns the user added are never flagged — this only reports
    columns afly needs that are missing or have a different type than
    expected, plus (see :attr:`partition_mismatch`) a partition key that
    doesn't match the configured granularity. ``alter_sql`` is populated only
    for the missing-columns case: a type change — and a partition-key
    change — has no safe automatic fix (ClickHouse can't ``ALTER`` a
    partition key in place; it means rebuilding the table), so afly never
    proposes one — see :class:`SchemaMismatchError`.
    """

    ok: bool
    missing: list[tuple[str, str]] = field(default_factory=list)
    type_mismatch: list[tuple[str, str, str]] = field(default_factory=list)
    partition_mismatch: str | None = None
    alter_sql: str | None = None


class SchemaMismatchError(Exception):
    """Raised by :func:`ensure_destination` when a live table doesn't match afly's schema.

    afly never ALTERs a table it didn't create, and never re-partitions one
    — the message says exactly what's wrong (missing/mismatched columns, or
    a partition key that doesn't match ``partition_granularity``) so the
    user (or their DBA) can act on it.
    """


def check_schema(
    manager: ClickHouseManager, db: str, table: str, granularity: Granularity = "month"
) -> SchemaCheck:
    rows = manager.query_dicts(
        "SELECT name, type FROM system.columns WHERE database = %(db)s AND table = %(table)s",
        {"db": db, "table": table},
    )
    existing = {r["name"]: r["type"] for r in rows}

    missing: list[tuple[str, str]] = []
    type_mismatch: list[tuple[str, str, str]] = []
    for name, ch_type in DESTINATION_COLUMNS:
        if name not in existing:
            missing.append((name, ch_type))
        elif _normalize_text(existing[name]) != _normalize_text(ch_type):
            type_mismatch.append((name, ch_type, existing[name]))

    partition_rows = manager.query_dicts(
        "SELECT partition_key FROM system.tables WHERE database = %(db)s AND name = %(table)s",
        {"db": db, "table": table},
    )
    partition_mismatch: str | None = None
    if partition_rows:
        existing_partition_key = partition_rows[0].get("partition_key")
        expected_partition_by = partition_by(granularity)
        if existing_partition_key and _normalize_text(existing_partition_key) != _normalize_text(
            expected_partition_by
        ):
            partition_mismatch = existing_partition_key

    alter_sql = None
    if missing:
        adds = ", ".join(f"ADD COLUMN {quote_ident(name)} {ch_type}" for name, ch_type in missing)
        alter_sql = f"ALTER TABLE {fqn(db, table)} {adds}"

    return SchemaCheck(
        ok=not missing and not type_mismatch and partition_mismatch is None,
        missing=missing,
        type_mismatch=type_mismatch,
        partition_mismatch=partition_mismatch,
        alter_sql=alter_sql,
    )


def ensure_destination(
    manager: ClickHouseManager, db: str, table: str, granularity: Granularity = "month"
) -> bool:
    """Create the destination if it doesn't exist yet; otherwise verify its schema.

    Returns ``True`` when the table was just created, ``False`` when it
    already existed and matched. Raises :class:`SchemaMismatchError` if an
    existing table is missing a column afly expects, has one with a
    different type, or was created with a different ``partition_granularity``
    than this run is configured for — afly writes to this table every run,
    and silently inserting against a drifted schema (or rebuilding partitions
    under the wrong ``PARTITION BY`` expression) is how you get
    truncated/garbage data instead of a loud failure at the one moment a
    human can still fix it.
    """
    if not manager.table_exists(db, table):
        manager.execute(destination_ddl(db, table, granularity))
        return True

    check = check_schema(manager, db, table, granularity)
    if not check.ok:
        parts = []
        if check.missing:
            parts.append("missing columns: " + ", ".join(name for name, _ in check.missing))
        if check.type_mismatch:
            parts.append(
                "type mismatches: "
                + ", ".join(
                    f"{name} (expected {expected}, got {actual})"
                    for name, expected, actual in check.type_mismatch
                )
            )
        if check.partition_mismatch is not None:
            parts.append(
                f"partition key mismatch: table was created with `{check.partition_mismatch}` "
                f"but partition_granularity={granularity!r} expects `{partition_by(granularity)}` "
                "— recreate the table or set partition_granularity to match it"
            )
        detail = "; ".join(parts)
        suffix = (
            f"\nRun this yourself — afly never ALTERs a user table:\n{check.alter_sql}"
            if check.alter_sql
            else ""
        )
        raise SchemaMismatchError(
            f"{fqn(db, table)} does not match afly's expected schema: {detail}.{suffix}"
        )
    return False
