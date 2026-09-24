"""Idempotent partition rebuild — the write path for the destination table.

ClickHouse 22.11 has neither a cheap ``DELETE FROM`` nor
``ReplacingMergeTree(ver, is_deleted)``, so "re-run a chunk without
duplicating rows" is implemented as: for each affected partition, rebuild the
whole partition in a staging table (old rows minus what's being replaced,
plus the fresh rows) and swap it in with ``REPLACE PARTITION`` — an atomic,
all-or-nothing operation at the partition level. See the module docstring's
"Warehouse" section in the M3 task spec for the exact statement order this
follows; it matters, not just the end state — see the empty-source-partition
note on :meth:`PartitionRebuilder.rebuild_partition` below.

A partition covers one or more calendar days (a whole month, under the
default ``partition_granularity: month`` — a single day under ``day``), so
the write path operates on *partitions* keyed by ``system.parts.partition_id``
(``afly.database.ddl.partition_id``), not on individual days — this is what
lets a month-wide partition rebuild in one ``REPLACE PARTITION`` regardless
of how many days of that month a given run actually touched.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from afly.database.clickhouse import ClickHouseManager
from afly.database.ddl import staging_table_name
from afly.schema import COLUMN_NAMES, Granularity, partition_by
from afly.utils.naming import fqn


@dataclass
class RebuildResult:
    partition_id: str
    kept_rows: int
    fresh_rows: int
    action: str  # "replace" | "drop" | "noop"


class PartitionRebuilder:
    def __init__(
        self,
        manager: ClickHouseManager,
        db: str,
        table: str,
        granularity: Granularity = "month",
        *,
        staging_db: str | None = None,
    ) -> None:
        self._manager = manager
        self._db = db
        self._table = table
        self._granularity = granularity
        # The staging table may live in another database than the destination:
        # a warehouse that mirrors every table of the destination database
        # (e.g. an automatic Distributed-wrapper sync over `raw`) would
        # otherwise publish afly's transient staging table too, and keep a
        # dangling wrapper after it is dropped. REPLACE PARTITION FROM works
        # across databases as long as the structure is identical.
        self._staging_db = staging_db or db

    @property
    def granularity(self) -> Granularity:
        return self._granularity

    @property
    def staging(self) -> str:
        # Qualify with the destination database when staging lives elsewhere, so
        # two destinations with the same table name never share a staging table.
        if self._staging_db != self._db:
            return staging_table_name(f"{self._db}__{self._table}")
        return staging_table_name(self._table)

    @property
    def staging_db(self) -> str:
        return self._staging_db

    def prepare_staging(self) -> None:
        """(Re)create the staging table from scratch, dropped first.

        Dropping first means a staging table left over with a stale schema
        (from before a destination ``ALTER TABLE ... ADD COLUMN``) can never
        make ``REPLACE PARTITION`` fail on a column mismatch — every run
        starts from a byte-for-byte copy of the destination's *current*
        schema.
        """
        if self._staging_db != self._db:
            self._manager.create_database(self._staging_db)
        self._manager.drop_table(self._staging_db, self.staging)
        self._manager.create_table_as(self._staging_db, self.staging, self._db, self._table)

    def drop_staging(self) -> None:
        self._manager.drop_table(self._staging_db, self.staging)

    def rows_for_pair(self, day: date, extract: str, app_id: str) -> int:
        """How many rows the destination already holds for (*day*, *extract*, *app_id*).

        Deliberately stays **day**-level regardless of ``partition_granularity``
        — this feeds M4's empty-response guard, which asks "did *this specific
        day* already have data for this pair", not "did the partition (which
        may be a whole month, under the default granularity) have any data
        anywhere in it". Filtering by the partition instead would let one
        already-populated day in a month mask an empty-response regression on
        a *different*, previously-empty day of that same month.

        Meant for M4's empty-response guard: an AppsFlyer pull that comes
        back with zero rows for a (extract, app_id, day) that previously had
        data is far more likely a transient API hiccup than "traffic really
        dropped to zero" — the caller can compare this count against the
        fresh pull before deciding whether an empty pull should actually
        clear the day.
        """
        return self._manager.count(
            self._db,
            self._table,
            where="date = %(day)s AND _extract = %(extract)s AND app_id = %(app_id)s",
            params={"day": day, "extract": extract, "app_id": app_id},
        )

    def rebuild_partition(
        self,
        partition_id: str,
        coverage: set[tuple[date, str, str]],
        fresh_rows: list[Mapping[str, Any]],
    ) -> RebuildResult:
        """Idempotently rewrite *partition_id*: old rows minus *coverage*, plus *fresh_rows*.

        *coverage* is the set of ``(day, extract, app_id)`` triples this call
        is authoritative for — their old rows are dropped from the rebuild
        regardless of whether ``fresh_rows`` actually contains anything for
        them (an empty pull for a covered triple means "zero is correct now",
        not "leave the old rows"). Triples *not* in coverage keep whatever the
        destination already had — this is how a month-wide partition can be
        rebuilt by several consecutive waves in one run (each wave's own
        days/pairs) without one wave's rebuild clobbering another's: every
        wave passes only the ``(day, extract, app_id)`` triples *it itself*
        just fetched, so a later wave's coverage never includes an earlier
        wave's days, and vice versa — see ``afly.run._rebuild.rebuild_wave``.
        An empty *coverage* means "kept = the whole partition" (no triple is
        being replaced) — for example a rebuild that's re-running a whole
        partition from scratch for every pair it knows about would pass its
        own full set here, not an empty one.

        Statement order, exactly:
        ``TRUNCATE staging`` → ``INSERT staging SELECT kept rows`` →
        ``INSERT staging VALUES fresh rows`` → ``REPLACE PARTITION`` (staging
        non-empty) / ``DROP PARTITION`` (staging empty, dest has the
        partition) / nothing (staging empty, dest doesn't have it either) →
        ``TRUNCATE staging`` in ``finally``.

        The empty-vs-nonempty branch is not an optimization, it's a
        correctness requirement: verified against a live 22.11 (and 26.3)
        server, ``ALTER TABLE dest REPLACE PARTITION ID 'X' FROM staging``
        when staging has **no part at all** for partition X does not error
        and does not leave *dest* untouched — it silently empties *dest*'s
        partition X, as if it never existed. So this method must never call
        REPLACE with an empty staging table; DROP (or nothing) is the only
        safe move when there's nothing to keep.
        """
        pid = partition_id
        try:
            self._manager.truncate(self._staging_db, self.staging)

            where = f"{partition_by(self._granularity)} = %(pid)s"
            params: dict[str, Any] = {"pid": int(pid)}
            if coverage:
                where += " AND (date, _extract, app_id) NOT IN %(triples)s"
                params["triples"] = sorted(coverage)
            self._manager.execute(
                f"INSERT INTO {fqn(self._staging_db, self.staging)} SELECT * FROM {fqn(self._db, self._table)} "
                f"WHERE {where}",
                params,
            )

            fresh_list = list(fresh_rows)
            fresh_count = 0
            if fresh_list:
                fresh_count = self._manager.insert_rows(
                    self._staging_db, self.staging, COLUMN_NAMES, fresh_list
                )

            total = self._manager.count(self._staging_db, self.staging)
            kept_rows = total - fresh_count

            if total > 0:
                self._manager.execute(
                    f"ALTER TABLE {fqn(self._db, self._table)} REPLACE PARTITION ID '{pid}' "
                    f"FROM {fqn(self._staging_db, self.staging)}"
                )
                action = "replace"
            elif self._manager.partition_exists(self._db, self._table, pid):
                self._manager.execute(
                    f"ALTER TABLE {fqn(self._db, self._table)} DROP PARTITION ID '{pid}'"
                )
                action = "drop"
            else:
                action = "noop"

            return RebuildResult(
                partition_id=pid, kept_rows=kept_rows, fresh_rows=fresh_count, action=action
            )
        finally:
            self._manager.truncate(self._staging_db, self.staging)
