"""DDL for afly's own bookkeeping tables: ``_afly_loads`` and ``_afly_locks``.

Column lists live here (not in ``afly.schema``, which is the *destination*
schema the CSV parser targets) — these two tables are afly's internal
state, never written by the parser, and their shape is owned entirely by
this milestone.
"""

from __future__ import annotations

from afly.database.clickhouse import ClickHouseManager
from afly.utils.naming import fqn, quote_ident

LOADS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("run_id", "String"),
    ("extract", "LowCardinality(String)"),
    ("app_id", "LowCardinality(String)"),
    ("report_type", "LowCardinality(String)"),
    ("from_date", "Date"),
    ("to_date", "Date"),
    ("chunk_days", "UInt8"),
    ("is_long", "UInt8"),
    ("status", "LowCardinality(String)"),
    ("rows", "UInt64"),
    ("api_calls", "UInt16"),
    ("http_status", "Nullable(UInt16)"),
    ("started_at", "DateTime64(3, 'UTC')"),
    ("finished_at", "Nullable(DateTime64(3, 'UTC'))"),
    ("duration_ms", "Nullable(UInt32)"),
    ("error", "Nullable(String)"),
    ("skip_reason", "Nullable(String)"),
    ("afly_version", "String"),
)

LOADS_COLUMN_NAMES: tuple[str, ...] = tuple(name for name, _ in LOADS_COLUMNS)

LOCKS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("lock_key", "String"),
    ("run_id", "String"),
    ("status", "LowCardinality(String)"),
    ("owner", "String"),
    ("started_at", "DateTime64(3, 'UTC')"),
    ("updated_at", "DateTime64(3, 'UTC')"),
    ("timeout_seconds", "UInt32"),
)

LOCKS_COLUMN_NAMES: tuple[str, ...] = tuple(name for name, _ in LOCKS_COLUMNS)


def _create_table_sql(
    db: str, table: str, columns: tuple[tuple[str, str], ...], engine_clause: str
) -> str:
    columns_sql = ",\n    ".join(f"{quote_ident(name)} {ch_type}" for name, ch_type in columns)
    return (
        f"CREATE TABLE IF NOT EXISTS {fqn(db, table)} (\n"
        f"    {columns_sql}\n"
        f")\n{engine_clause}"
    )


def loads_ddl(db: str, table: str) -> str:
    """Append-only ledger: one row per chunk-load attempt (start + finish)."""
    return _create_table_sql(
        db,
        table,
        LOADS_COLUMNS,
        "ENGINE = MergeTree\nPARTITION BY toYYYYMM(started_at)\nORDER BY (extract, app_id, from_date, started_at)",
    )


def locks_ddl(db: str, table: str) -> str:
    """Distributed run locks — ``ReplacingMergeTree`` collapsed on read via ``FINAL``."""
    return _create_table_sql(
        db,
        table,
        LOCKS_COLUMNS,
        "ENGINE = ReplacingMergeTree(updated_at)\nORDER BY lock_key",
    )


def ensure_internal_tables(
    manager: ClickHouseManager, internal_db: str, loads_table: str, locks_table: str
) -> None:
    """Create the internal database and both bookkeeping tables if missing."""
    manager.create_database(internal_db)
    manager.execute(loads_ddl(internal_db, loads_table))
    manager.execute(locks_ddl(internal_db, locks_table))
