"""``afly debug --deep``'s ClickHouse probes — connectivity plus a live REPLACE/DROP drill.

Basic checks establish "can afly reach this warehouse and use it at all";
the deep checks additionally prove the exact partition-swap mechanics
:mod:`afly.database.writer` relies on actually behave the way it assumes on
*this* server, using two disposable ``__afly_debug_*`` tables that are always
dropped again in a ``finally``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from afly.config.profile import ClickHouseProfile
from afly.database.clickhouse import ClickHouseError, ClickHouseManager
from afly.database.ddl import destination_ddl, partition_id
from afly.schema import COLUMN_NAMES, DESTINATION_COLUMNS, Granularity
from afly.utils.datetime_utils import now_utc
from afly.utils.naming import fqn

_DEBUG_TABLE_A = "__afly_debug_a"
_DEBUG_TABLE_B = "__afly_debug_b"
_GRANT_MARKERS = ("ALTER", "INSERT", "CREATE TABLE")


@dataclass
class CheckOutcome:
    name: str
    ok: bool
    detail: str


def _sample_row(
    *, app_id: str, day: date, extract: str, event_counter: dict[str, int]
) -> dict[str, Any]:
    """A minimal-but-complete row matching every ``DESTINATION_COLUMNS`` key.

    Nullable metric columns are left ``None``; the Map columns default to
    ``{}`` except ``event_counter``, which the REPLACE-PARTITION drill uses
    to prove Map values round-trip through the swap, not just row counts.
    """
    row: dict[str, Any] = dict.fromkeys(name for name, _ in DESTINATION_COLUMNS)
    row.update(
        app_id=app_id,
        date=day,
        report_type="app_id_report",
        category="standard",
        is_retargeting=0,
        agency="",
        media_source="",
        campaign="",
        campaign_name="",
        campaign_id="",
        adset="",
        adset_id="",
        adgroup="",
        adgroup_id="",
        country="",
        currency="",
        event_unique_users={},
        event_counter=event_counter,
        event_sales={},
        extra={},
        _extract=extract,
        _run_id="afly-debug-check",
        _loaded_at=now_utc(),
    )
    return row


def run_clickhouse_checks(
    profile: ClickHouseProfile, *, deep: bool, granularity: Granularity = "month"
) -> list[CheckOutcome]:
    """Run the basic (always) and deep (``--deep``) ClickHouse probes for ``afly debug``.

    *granularity* only affects the deep checks (the DDL/partition-swap drill
    against disposable tables) — it's the project's configured
    ``defaults.partition_granularity``, defaulting to ``"month"`` when the
    caller has no project loaded (e.g. a bare profile check).
    """
    outcomes: list[CheckOutcome] = []

    try:
        manager = ClickHouseManager.from_profile(profile)
    except Exception as exc:  # pragma: no cover - Client() rarely raises at construction
        outcomes.append(CheckOutcome(name="connect", ok=False, detail=str(exc)))
        return outcomes

    try:
        try:
            version = manager.server_version()
        except ClickHouseError as exc:
            outcomes.append(CheckOutcome(name="connect", ok=False, detail=str(exc)))
            return outcomes
        outcomes.append(
            CheckOutcome(
                name="connect",
                ok=True,
                detail=(
                    f"server version {version} via {profile.protocol}:{profile.effective_port}"
                ),
            )
        )

        try:
            rows = manager.execute("SELECT 1")
            outcomes.append(CheckOutcome(name="select_1", ok=rows == [(1,)], detail=str(rows)))
        except ClickHouseError as exc:
            outcomes.append(CheckOutcome(name="select_1", ok=False, detail=str(exc)))

        for label, db in (
            ("database", profile.database),
            ("internal_database", profile.internal_db),
        ):
            try:
                manager.create_database(db)
                outcomes.append(
                    CheckOutcome(name=f"{label}_exists_or_creatable", ok=True, detail=db)
                )
            except ClickHouseError as exc:
                outcomes.append(
                    CheckOutcome(name=f"{label}_exists_or_creatable", ok=False, detail=str(exc))
                )

        try:
            grants = manager.execute("SHOW GRANTS")
            text = " ".join(str(row) for row in grants).upper()
            # `GRANT ALL ON *.*` (a typical admin user) names none of the
            # individual privileges, but implies every one afly needs.
            found = (
                ["ALL"]
                if "GRANT ALL ON" in text
                else [marker for marker in _GRANT_MARKERS if marker in text]
            )
            detail = (
                f"found: {', '.join(found)}"
                if found
                else "no ALTER/INSERT/CREATE TABLE grant text matched"
            )
            outcomes.append(CheckOutcome(name="grants", ok=True, detail=detail))
        except Exception as exc:  # SHOW GRANTS can be restricted entirely — never fatal
            outcomes.append(
                CheckOutcome(name="grants", ok=True, detail=f"grants not readable: {exc}")
            )

        if deep:
            outcomes.extend(_deep_checks(manager, profile.database, granularity))
    finally:
        manager.close()

    return outcomes


def _deep_checks(
    manager: ClickHouseManager, db: str, granularity: Granularity
) -> list[CheckOutcome]:
    outcomes: list[CheckOutcome] = []
    day1 = date(2026, 1, 1)
    # day2 must land in a DIFFERENT partition than day1 under *granularity* —
    # under "month" (the default) a plain day1+1 would still be January 2026,
    # which the deep_replace_from_missing_partition drill below needs to NOT
    # be true (it's testing REPLACE FROM a source with no part for the target
    # partition at all).
    day2 = date(2026, 2, 1) if granularity == "month" else date(2026, 1, 2)
    pid1 = partition_id(day1, granularity)
    pid2 = partition_id(day2, granularity)

    try:
        manager.drop_table(db, _DEBUG_TABLE_A)
        manager.drop_table(db, _DEBUG_TABLE_B)
        manager.execute(destination_ddl(db, _DEBUG_TABLE_A, granularity))
        manager.execute(destination_ddl(db, _DEBUG_TABLE_B, granularity))
        outcomes.append(
            CheckOutcome(
                name="deep_create_tables", ok=True, detail=f"{_DEBUG_TABLE_A}, {_DEBUG_TABLE_B}"
            )
        )

        rows_a = [
            _sample_row(
                app_id="app1", day=day1, extract="deep_check", event_counter={"install": 1}
            ),
            _sample_row(
                app_id="app2", day=day1, extract="deep_check", event_counter={"install": 2}
            ),
        ]
        rows_b = [
            _sample_row(app_id="app1", day=day1, extract="deep_check", event_counter={"install": 9})
        ]
        manager.insert_rows(db, _DEBUG_TABLE_A, COLUMN_NAMES, rows_a)
        manager.insert_rows(db, _DEBUG_TABLE_B, COLUMN_NAMES, rows_b)
        outcomes.append(
            CheckOutcome(name="deep_insert_sample_rows", ok=True, detail="A=2 rows, B=1 row")
        )

        manager.execute(
            f"ALTER TABLE {fqn(db, _DEBUG_TABLE_A)} REPLACE PARTITION ID '{pid1}' FROM {fqn(db, _DEBUG_TABLE_B)}"
        )
        after_replace = manager.query_dicts(f"SELECT * FROM {fqn(db, _DEBUG_TABLE_A)}")
        map_ok = bool(after_replace) and after_replace[0].get("event_counter") == {"install": 9}
        ok = len(after_replace) == 1 and map_ok
        outcomes.append(
            CheckOutcome(
                name="deep_replace_partition",
                ok=ok,
                detail=(
                    f"A has {len(after_replace)} row(s) after REPLACE PARTITION FROM B "
                    f"(expected 1); event_counter round-tripped: {map_ok}"
                ),
            )
        )

        manager.execute(f"ALTER TABLE {fqn(db, _DEBUG_TABLE_A)} DROP PARTITION ID '{pid1}'")
        count_after_drop = manager.count(db, _DEBUG_TABLE_A)
        outcomes.append(
            CheckOutcome(
                name="deep_drop_partition",
                ok=count_after_drop == 0,
                detail=f"A count={count_after_drop}",
            )
        )

        # Informational only: B has no rows (and thus no part) for pid2. This
        # is exactly the situation afly's writer must never create on
        # purpose — see the docstring of PartitionRebuilder.rebuild_partition.
        try:
            manager.execute(
                f"ALTER TABLE {fqn(db, _DEBUG_TABLE_A)} REPLACE PARTITION ID '{pid2}' FROM {fqn(db, _DEBUG_TABLE_B)}"
            )
            count_after_empty_replace = manager.count(db, _DEBUG_TABLE_A)
            outcomes.append(
                CheckOutcome(
                    name="deep_replace_from_missing_partition",
                    ok=True,
                    detail=(
                        "REPLACE PARTITION FROM a source lacking that partition did NOT raise "
                        f"on this server — A count is now {count_after_empty_replace} "
                        "(afly's writer treats an empty staging table as a signal to DROP/no-op "
                        "instead, precisely to avoid relying on this)"
                    ),
                )
            )
        except ClickHouseError as exc:
            outcomes.append(
                CheckOutcome(
                    name="deep_replace_from_missing_partition",
                    ok=True,
                    detail=f"REPLACE PARTITION FROM a source lacking that partition raised on this server: {exc}",
                )
            )
    finally:
        manager.drop_table(db, _DEBUG_TABLE_A)
        manager.drop_table(db, _DEBUG_TABLE_B)

    return outcomes
