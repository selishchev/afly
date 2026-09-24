"""Implementation of ``afly unlock`` — clear stale/held extract locks.

Separate from ``run``/``debug`` because it deliberately bypasses the normal
staleness check in :meth:`afly.database.locks.LocksRepo.acquire` — this is
the manual escape hatch for "a run died mid-flight and left a lock a human
now knows is safe to clear", not something ``run`` itself would ever call.
"""

from __future__ import annotations

from afly.cli._output import echo_done, echo_error, echo_noop, echo_tree
from afly.cli._project import ProjectError, load_context
from afly.config import ConfigError
from afly.database.clickhouse import ClickHouseError, ClickHouseManager
from afly.database.locks import LocksRepo
from afly.database.tables import ensure_internal_tables
from afly.utils.naming import qualify_table


def run_unlock(table: str | None, all_: bool, profile: str | None) -> int:
    """Clear one lock (``--table``) or every active lock (``--all``).

    Returns a process exit code (0 success, 1 failure) — ``cli/main.py``
    turns a non-zero return into ``sys.exit``.
    """
    if bool(table) == bool(all_):
        echo_error("specify exactly one of --table or --all")
        return 1

    try:
        ctx = load_context(profile)
    except (ProjectError, ConfigError) as exc:
        echo_error(str(exc))
        return 1

    ch_profile = ctx.profile.clickhouse
    try:
        manager = ClickHouseManager.from_profile(ch_profile)
    except Exception as exc:
        echo_error(f"could not connect to ClickHouse: {exc}")
        return 1

    try:
        ensure_internal_tables(
            manager, ch_profile.internal_db, ctx.project.tables.loads, ctx.project.tables.locks
        )
        locks = LocksRepo(manager, ch_profile.internal_db, ctx.project.tables.locks)

        if table:
            db, tbl = qualify_table(table, ch_profile.database)
            lock_key = f"table:{db}.{tbl}"
            if locks.clear(lock_key):
                echo_done(f"cleared lock {lock_key}")
            else:
                echo_done(f"no active lock held on {lock_key}")
            return 0

        active = locks.list_active()
        if not active:
            echo_noop("unlock", "no active locks")
            return 0

        cleared = [row["lock_key"] for row in active if locks.clear(row["lock_key"])]
        if cleared:
            echo_tree("unlock", [f"{key}: cleared" for key in cleared])
        else:
            echo_noop("unlock", "no active locks left to clear (cleared concurrently)")
        echo_done(f"cleared {len(cleared)} lock(s)")
        return 0
    except ClickHouseError as exc:
        echo_error(str(exc))
        return 1
    finally:
        manager.close()
