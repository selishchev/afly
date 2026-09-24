"""ClickHouse table-name helpers: qualifying, quoting, and fully-qualifying.

An extract's ``table:`` field may be bare (``appsflyer_geo_by_date``, resolved
against the profile's database) or db-qualified (``other_db.some_table``) —
this module is the single place that grammar is parsed and rendered back into
SQL-safe identifiers, so ``run``/``debug``/``unlock`` (owned by other
milestones) and ``ls``/``validate`` (owned here) agree on it byte-for-byte.
"""

from __future__ import annotations


def qualify_table(name: str, default_db: str) -> tuple[str, str]:
    """Split a possibly db-qualified table name into ``(database, table)``.

    ``"db.tbl"`` splits on the single dot; a bare ``"tbl"`` is resolved
    against *default_db*. Anything else (no dot and empty, or more than one
    dot, or an empty database/table part) is rejected — silently accepting
    e.g. ``"a.b.c"`` would pick an arbitrary interpretation and mask a typo
    in the config.
    """
    if not name or not name.strip():
        raise ValueError("table name must not be empty")

    parts = name.split(".")
    if len(parts) == 1:
        table = parts[0]
        if not table:
            raise ValueError(f"invalid table name {name!r}")
        return default_db, table
    if len(parts) == 2:
        database, table = parts
        if not database or not table:
            raise ValueError(f"invalid table name {name!r} — expected 'table' or 'database.table'")
        return database, table
    raise ValueError(
        f"invalid table name {name!r} — expected 'table' or 'database.table', "
        "found more than one '.'"
    )


def quote_ident(s: str) -> str:
    """Backtick-quote a ClickHouse identifier, escaping any embedded backtick."""
    return "`" + s.replace("`", "``") + "`"


def fqn(db: str, table: str) -> str:
    """Fully-qualified, backtick-quoted ``` `db`.`table` ``` for use in SQL."""
    return f"{quote_ident(db)}.{quote_ident(table)}"
