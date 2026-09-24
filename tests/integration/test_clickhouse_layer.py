"""End-to-end ClickHouse-layer tests against a real server (testcontainers).

Covers what FakeManager-based unit tests can't: genuine ``system.columns``/
``system.tables.partition_key`` behaviour, genuine ``ALTER TABLE ...
REPLACE/DROP PARTITION`` semantics (including the empty-source-partition
trap) under both configurable ``partition_granularity`` values,
``ReplacingMergeTree ... FINAL`` collapsing for locks, and the append-only
``_afly_loads`` ledger.

Every test in this module is parametrized (via the ``manager``/``test_db``
fixtures in ``conftest.py``) over both the server image (22.11, 26.3 —
``AFLY_CH_IMAGES``) and the transport (``native``/``http``) — the same
assertions run against ``clickhouse_driver`` and
``afly.database._http_client.HttpClient`` alike, against both the oldest
server this project still targets and the analyst's actual 26.3 warehouse.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from afly.database.checks import run_clickhouse_checks
from afly.database.clickhouse import ClickHouseManager
from afly.database.ddl import SchemaMismatchError, check_schema, ensure_destination, partition_id
from afly.database.loads import ChunkLoad, LoadsRepo
from afly.database.locks import LockHeldError, LocksRepo
from afly.database.tables import ensure_internal_tables
from afly.database.writer import PartitionRebuilder
from afly.schema import COLUMN_NAMES, DESTINATION_COLUMNS
from afly.utils.datetime_utils import now_utc

pytestmark = pytest.mark.integration

_TABLE = "af_reports"


def _row(*, app_id: str, day: date, extract: str, marker: str = "") -> dict:
    """A full destination row, every column populated, tagged by *marker*.

    *marker* (written into ``media_source``) lets a test distinguish "old"
    from "new" rows after a rebuild without relying on row order.
    """
    row: dict = dict.fromkeys(name for name, _ in DESTINATION_COLUMNS)
    row.update(
        app_id=app_id,
        date=day,
        report_type="app_id_report",
        category="standard",
        is_retargeting=0,
        agency="",
        media_source=marker,
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
        event_counter={},
        event_sales={},
        extra={},
        _extract=extract,
        _run_id="it-run",
        _loaded_at=now_utc(),
    )
    return row


def _rows_for_day(manager: ClickHouseManager, test_db: str, day: date) -> list[dict]:
    """Every row for *day*, regardless of which (month- or day-wide) partition it sits in."""
    return manager.query_dicts(
        f"SELECT * FROM `{test_db}`.`{_TABLE}` WHERE date = %(day)s ORDER BY _extract, app_id",
        {"day": day},
    )


def _sortable(rows: list[dict]) -> list[tuple]:
    return sorted(tuple(sorted(r.items())) for r in rows)


# ── (1) ensure_destination / check_schema round trip ────────────────────────


@pytest.mark.integration
def test_ensure_destination_creates_then_confirms(manager: ClickHouseManager, test_db: str) -> None:
    created_first = ensure_destination(manager, test_db, _TABLE)
    assert created_first is True
    assert manager.table_exists(test_db, _TABLE)

    created_second = ensure_destination(manager, test_db, _TABLE)
    assert created_second is False

    check = check_schema(manager, test_db, _TABLE)
    assert check.ok is True
    assert check.missing == []
    assert check.type_mismatch == []
    assert check.partition_mismatch is None


@pytest.mark.integration
def test_ensure_destination_creates_with_day_granularity(
    manager: ClickHouseManager, test_db: str
) -> None:
    ensure_destination(manager, test_db, _TABLE, "day")

    check = check_schema(manager, test_db, _TABLE, "day")
    assert check.ok is True

    partition_key = manager.query_dicts(
        "SELECT partition_key FROM system.tables WHERE database = %(db)s AND name = %(table)s",
        {"db": test_db, "table": _TABLE},
    )[0]["partition_key"]
    assert "toYYYYMMDD" in partition_key


@pytest.mark.integration
def test_ensure_destination_raises_on_partition_granularity_mismatch(
    manager: ClickHouseManager, test_db: str
) -> None:
    """A table created under one granularity, then opened under the other,
    is a hard failure — afly never re-partitions a table (ClickHouse has no
    in-place ALTER for a MergeTree's PARTITION BY)."""
    ensure_destination(manager, test_db, _TABLE, "day")

    with pytest.raises(SchemaMismatchError) as excinfo:
        ensure_destination(manager, test_db, _TABLE, "month")

    message = str(excinfo.value)
    assert "partition key mismatch" in message
    assert "toYYYYMMDD" in message
    assert "toYYYYMM(date)" in message

    check = check_schema(manager, test_db, _TABLE, "month")
    assert check.ok is False
    assert check.partition_mismatch is not None
    assert "toYYYYMMDD" in check.partition_mismatch


# ── (2) month-partition rebuild: one day's rebuild leaves other days of the
#        same month untouched, and a later rebuild with fewer rows replaces
#        only the covered pair ──────────────────────────────────────────────


@pytest.mark.integration
def test_rebuild_partition_month_grain_replaces_only_the_covered_pair(
    manager: ClickHouseManager, test_db: str
) -> None:
    ensure_destination(manager, test_db, _TABLE)  # default: month
    rebuilder = PartitionRebuilder(manager, test_db, _TABLE)
    assert rebuilder.granularity == "month"
    rebuilder.prepare_staging()

    day1, day2 = date(2026, 9, 10), date(2026, 9, 11)  # same month partition
    pid = partition_id(day1, "month")
    assert pid == partition_id(day2, "month")  # sanity: genuinely one partition

    # Run 1: day1 gets 2 facebook/app1 rows + 1 standard/app1 + 1 standard/app2;
    # day2 gets 1 standard/app1 + 1 facebook/app1. All coverage-tracked (a
    # normal first load: every pair present is authoritative).
    run1_rows = [
        _row(app_id="app1", day=day1, extract="facebook", marker="run1-fb-1"),
        _row(app_id="app1", day=day1, extract="facebook", marker="run1-fb-2"),
        _row(app_id="app1", day=day1, extract="standard", marker="run1-std-app1"),
        _row(app_id="app2", day=day1, extract="standard", marker="run1-std-app2"),
        _row(app_id="app1", day=day2, extract="standard", marker="run1-day2-std"),
        _row(app_id="app1", day=day2, extract="facebook", marker="run1-day2-fb"),
    ]
    coverage1 = {
        (day1, "facebook", "app1"),
        (day1, "standard", "app1"),
        (day1, "standard", "app2"),
        (day2, "standard", "app1"),
        (day2, "facebook", "app1"),
    }
    result1 = rebuilder.rebuild_partition(pid, coverage1, run1_rows)
    assert result1.action == "replace"
    assert result1.partition_id == pid
    assert result1.fresh_rows == 6

    standard_before = [
        r for r in _rows_for_day(manager, test_db, day1) if r["_extract"] == "standard"
    ]
    day2_before = _rows_for_day(manager, test_db, day2)

    # Run 2: only rebuild day1, only facebook/app1 is covered (the other pair
    # "failed" this run and is left alone), with FEWER facebook rows (1, was 2).
    rebuilder.prepare_staging()
    fresh_facebook = [_row(app_id="app1", day=day1, extract="facebook", marker="run2-fb-1")]
    result2 = rebuilder.rebuild_partition(pid, {(day1, "facebook", "app1")}, fresh_facebook)
    assert result2.action == "replace"
    assert result2.fresh_rows == 1
    assert result2.kept_rows == 4  # 2 standard (day1) + 2 rows (day2), the day2 rows untouched

    day1_after = _rows_for_day(manager, test_db, day1)
    facebook_after = [r for r in day1_after if r["_extract"] == "facebook"]
    standard_after = [r for r in day1_after if r["_extract"] == "standard"]

    assert len(day1_after) == 3  # 2 standard (kept) + 1 facebook (replaced)
    assert [r["media_source"] for r in facebook_after] == ["run2-fb-1"]
    assert _sortable(standard_after) == _sortable(standard_before)

    # day2 — a DIFFERENT day of the SAME month partition — is completely
    # untouched by day1's rebuild. This is the crux of configurable
    # granularity: the partition swap is scoped by (date, _extract, app_id)
    # coverage, not by "everything in this partition".
    day2_after = _rows_for_day(manager, test_db, day2)
    assert _sortable(day2_after) == _sortable(day2_before)


# ── (3) a pair excluded from coverage (e.g. its pull failed) keeps its old rows ──


@pytest.mark.integration
def test_rebuild_partition_pair_excluded_from_coverage_is_untouched(
    manager: ClickHouseManager, test_db: str
) -> None:
    ensure_destination(manager, test_db, _TABLE)
    rebuilder = PartitionRebuilder(manager, test_db, _TABLE)
    rebuilder.prepare_staging()

    day = date(2026, 9, 12)
    pid = partition_id(day, "month")
    rebuilder.rebuild_partition(
        pid,
        {(day, "standard", "app1"), (day, "yandex", "app1")},
        [
            _row(app_id="app1", day=day, extract="standard", marker="v1-standard"),
            _row(app_id="app1", day=day, extract="yandex", marker="v1-yandex"),
        ],
    )

    # This run's yandex/app1 pull failed — only "standard" is covered/fresh.
    rebuilder.prepare_staging()
    result = rebuilder.rebuild_partition(
        pid,
        {(day, "standard", "app1")},
        [_row(app_id="app1", day=day, extract="standard", marker="v2-standard")],
    )
    assert result.action == "replace"
    assert result.kept_rows == 1  # the untouched yandex row

    rows = _rows_for_day(manager, test_db, day)
    by_extract = {r["_extract"]: r for r in rows}
    assert by_extract["standard"]["media_source"] == "v2-standard"
    assert by_extract["yandex"]["media_source"] == "v1-yandex"  # untouched


# ── (4) Map columns round-trip ──────────────────────────────────────────────


@pytest.mark.integration
def test_map_columns_round_trip(manager: ClickHouseManager, test_db: str) -> None:
    ensure_destination(manager, test_db, _TABLE)
    day = date(2026, 9, 13)
    row_nonempty = _row(app_id="app1", day=day, extract="standard", marker="maps")
    row_nonempty["event_counter"] = {"install": 3, "purchase": 1}
    row_nonempty["event_sales"] = {"purchase": 9.99}
    row_nonempty["extra"] = {"note": "hello"}
    row_empty = _row(app_id="app2", day=day, extract="standard", marker="maps-empty")

    manager.insert_rows(test_db, _TABLE, COLUMN_NAMES, [row_nonempty, row_empty])

    rows = manager.query_dicts(
        f"SELECT * FROM `{test_db}`.`{_TABLE}` WHERE date = %(day)s ORDER BY app_id", {"day": day}
    )
    assert rows[0]["event_counter"] == {"install": 3, "purchase": 1}
    assert rows[0]["event_sales"] == {"purchase": 9.99}
    assert rows[0]["extra"] == {"note": "hello"}
    assert rows[1]["event_counter"] == {}
    assert rows[1]["event_unique_users"] == {}


# ── (5) identical rows: MergeTree has no dedup, both must land ─────────────


@pytest.mark.integration
def test_two_identical_rows_are_both_stored(manager: ClickHouseManager, test_db: str) -> None:
    """Product requirement: AppsFlyer can return two rows identical on every
    dimension within one pull, and both must count — MergeTree has no
    dedup, unlike a ReplacingMergeTree collapse."""
    ensure_destination(manager, test_db, _TABLE)
    rebuilder = PartitionRebuilder(manager, test_db, _TABLE)
    rebuilder.prepare_staging()

    day = date(2026, 9, 16)
    identical = _row(app_id="app1", day=day, extract="standard", marker="dup")
    fresh_rows = [dict(identical), dict(identical)]

    result = rebuilder.rebuild_partition(
        partition_id(day, "month"), {(day, "standard", "app1")}, fresh_rows
    )

    assert result.fresh_rows == 2
    rows = _rows_for_day(manager, test_db, day)
    assert len(rows) == 2
    assert rows[0]["media_source"] == rows[1]["media_source"] == "dup"


# ── (6) rebuild_partition ends up empty -> DROP (whole month becomes empty) ─


@pytest.mark.integration
def test_rebuild_partition_drops_when_the_whole_month_becomes_empty(
    manager: ClickHouseManager, test_db: str
) -> None:
    ensure_destination(manager, test_db, _TABLE)
    rebuilder = PartitionRebuilder(manager, test_db, _TABLE)
    rebuilder.prepare_staging()

    day = date(2026, 9, 14)
    pid = partition_id(day, "month")
    rebuilder.rebuild_partition(
        pid, {(day, "standard", "app1")}, [_row(app_id="app1", day=day, extract="standard")]
    )
    assert manager.partition_exists(test_db, _TABLE, pid)

    # This run's pull for every pair that existed in the month came back
    # empty — coverage names everything that WAS there, fresh_rows is empty,
    # so nothing survives into staging and the whole partition is dropped.
    rebuilder.prepare_staging()
    result = rebuilder.rebuild_partition(pid, {(day, "standard", "app1")}, [])

    assert result.action == "drop"
    assert result.kept_rows == 0
    assert result.fresh_rows == 0
    assert not manager.partition_exists(test_db, _TABLE, pid)
    assert manager.count(test_db, _TABLE, where="date = %(day)s", params={"day": day}) == 0


@pytest.mark.integration
def test_rebuild_partition_noop_on_a_partition_that_never_had_data(
    manager: ClickHouseManager, test_db: str
) -> None:
    """Complements the drop test above: truly empty coverage on a never-touched partition is a no-op."""
    ensure_destination(manager, test_db, _TABLE)
    rebuilder = PartitionRebuilder(manager, test_db, _TABLE)
    rebuilder.prepare_staging()

    pid = partition_id(date(2026, 9, 15), "month")
    result = rebuilder.rebuild_partition(pid, set(), [])

    assert result.action == "noop"
    assert not manager.partition_exists(test_db, _TABLE, pid)


@pytest.mark.integration
def test_rebuild_partition_day_grain_drop_and_replace(
    manager: ClickHouseManager, test_db: str
) -> None:
    """The day-granularity path end to end: two adjacent days, each its own
    partition, a rebuild of one never touches the other."""
    ensure_destination(manager, test_db, _TABLE, "day")
    rebuilder = PartitionRebuilder(manager, test_db, _TABLE, "day")
    rebuilder.prepare_staging()

    day1, day2 = date(2026, 9, 20), date(2026, 9, 21)
    pid1, pid2 = partition_id(day1, "day"), partition_id(day2, "day")
    assert pid1 != pid2

    rebuilder.rebuild_partition(
        pid1, {(day1, "standard", "app1")}, [_row(app_id="app1", day=day1, extract="standard")]
    )
    rebuilder.prepare_staging()
    rebuilder.rebuild_partition(
        pid2, {(day2, "standard", "app1")}, [_row(app_id="app1", day=day2, extract="standard")]
    )
    day2_before = _rows_for_day(manager, test_db, day2)

    # Drop day1 entirely — day2 (a DIFFERENT partition, under day granularity)
    # must be completely unaffected.
    rebuilder.prepare_staging()
    result = rebuilder.rebuild_partition(pid1, {(day1, "standard", "app1")}, [])

    assert result.action == "drop"
    assert not manager.partition_exists(test_db, _TABLE, pid1)
    assert manager.partition_exists(test_db, _TABLE, pid2)
    assert _sortable(_rows_for_day(manager, test_db, day2)) == _sortable(day2_before)


# ── (7) check_schema after real ALTER TABLE ADD/DROP COLUMN ────────────────


@pytest.mark.integration
def test_check_schema_after_add_column_is_ok(manager: ClickHouseManager, test_db: str) -> None:
    ensure_destination(manager, test_db, _TABLE)
    manager.execute(f"ALTER TABLE `{test_db}`.`{_TABLE}` ADD COLUMN `extra_user_col` String")

    check = check_schema(manager, test_db, _TABLE)

    assert check.ok is True


@pytest.mark.integration
def test_check_schema_after_drop_column_reports_missing_with_alter_sql(
    manager: ClickHouseManager, test_db: str
) -> None:
    ensure_destination(manager, test_db, _TABLE)
    manager.execute(f"ALTER TABLE `{test_db}`.`{_TABLE}` DROP COLUMN `roi`")

    check = check_schema(manager, test_db, _TABLE)

    assert check.ok is False
    assert any(name == "roi" for name, _ in check.missing)
    assert check.alter_sql is not None
    assert "roi" in check.alter_sql
    assert f"`{test_db}`.`{_TABLE}`" in check.alter_sql


# ── (8) locks: held / stale / force / clear ─────────────────────────────────


@pytest.mark.integration
def test_locks_held_stale_force_clear(manager: ClickHouseManager, test_db: str) -> None:
    ensure_internal_tables(manager, test_db, "_afly_loads", "_afly_locks")
    locks = LocksRepo(manager, test_db, "_afly_locks")
    lock_key = "table:marts.af_reports"

    # run-1 acquires with a SHORT timeout — staleness is judged against the
    # HOLDER's own recorded timeout_seconds, not whatever a later caller asks
    # for, so this is what actually makes the lock go stale below.
    locks.acquire(lock_key, "run-1", "owner-1", timeout_seconds=1)

    with pytest.raises(LockHeldError) as excinfo:
        locks.acquire(lock_key, "run-2", "owner-2", timeout_seconds=3600)
    assert excinfo.value.run_id == "run-1"

    # Once run-1's own 1-second timeout has elapsed, its lock is stale.
    import time

    time.sleep(1.1)
    locks.acquire(
        lock_key, "run-2", "owner-2", timeout_seconds=3600
    )  # must not raise: stale, overridden
    active = {row["run_id"]: row for row in locks.list_active()}
    assert "run-2" in active

    # force takes it unconditionally even though run-2's lock is fresh.
    locks.acquire(lock_key, "run-3", "owner-3", timeout_seconds=3600, force=True)
    active = {row["run_id"]: row for row in locks.list_active()}
    assert "run-3" in active

    cleared = locks.clear(lock_key)
    assert cleared is True
    assert locks.list_active() == []
    assert locks.clear(lock_key) is False  # nothing left to clear


# ── (9) loads: watermark + long_calls_today ─────────────────────────────────


@pytest.mark.integration
def test_loads_watermark_and_long_calls_today(manager: ClickHouseManager, test_db: str) -> None:
    ensure_internal_tables(manager, test_db, "_afly_loads", "_afly_locks")
    repo = LoadsRepo(manager, test_db, "_afly_loads")

    assert repo.watermark("standard", "app1") is None

    load = ChunkLoad(
        run_id="run-1",
        extract="standard",
        app_id="app1",
        report_type="app_id_report",
        from_date=date(2026, 9, 1),
        to_date=date(2026, 9, 3),
        chunk_days=2,
        is_long=True,
    )
    started = now_utc()
    repo.start_chunk(load, started)
    repo.finish_chunk(
        load,
        "success",
        started_at=started,
        finished_at=started + timedelta(seconds=5),
        rows=100,
        api_calls=2,
    )

    assert repo.watermark("standard", "app1") == date(2026, 9, 3)

    # A still-running long chunk must not count toward today's quota.
    running_load = ChunkLoad(
        run_id="run-2",
        extract="standard",
        app_id="app1",
        report_type="app_id_report",
        from_date=date(2026, 9, 4),
        to_date=date(2026, 9, 10),
        chunk_days=6,
        is_long=True,
    )
    repo.start_chunk(running_load, now_utc())

    other_app_load = ChunkLoad(
        run_id="run-3",
        extract="standard",
        app_id="app2",
        report_type="app_id_report",
        from_date=date(2026, 9, 1),
        to_date=date(2026, 9, 5),
        chunk_days=4,
        is_long=True,
    )
    now = now_utc()
    repo.start_chunk(other_app_load, now)
    repo.finish_chunk(
        other_app_load, "success", started_at=now, finished_at=now, rows=10, api_calls=1
    )

    # started_at above is `now_utc()`-based (not a fixed calendar date), so
    # query with today's real date.
    total, per_app = repo.long_calls_today(now_utc().date())
    assert per_app == {"app1": 2, "app2": 1}
    assert total == 3


# ── (10) run_clickhouse_checks(deep=True) ────────────────────────────────────


@pytest.mark.integration
def test_deep_checks_all_ok_and_report_empty_replace_behaviour(
    clickhouse_container, protocol: str, test_db: str  # type: ignore[no-untyped-def]
) -> None:
    """Runs the deep checks over *this test's own* `protocol`, not always native.

    `run_clickhouse_checks` takes a `ClickHouseProfile`, not a `ClickHouseManager`
    — it builds its own client internally — so this test can't reuse the
    `manager` fixture directly and instead builds a profile via the same
    `profile_for` helper `manager` uses, keeping it on the axis pytest
    already parametrized this test over (2 images x 2 protocols). Exercises
    the configured granularity default (`"month"` — the same default
    `run_clickhouse_checks` and `afly debug --deep` use when no project's
    `defaults.partition_granularity` is passed).

    `deep_replace_from_missing_partition` is informational-only by design
    (see its `CheckOutcome` in `afly.database.checks`) precisely because the
    exact behaviour of `REPLACE PARTITION FROM` a source lacking that
    partition is not guaranteed across server versions — this test only
    asserts the check *ran and reported something*, not which behaviour it
    observed. Measured across the parametrized images (2026-09):
    `clickhouse/clickhouse-server:22.11` and `:26.3` both silently empty the
    target partition rather than raising — the writer's DROP-instead-of-REPLACE
    rule (`PartitionRebuilder.rebuild_partition`) is exercised directly by the
    other tests in this file and is what actually guards against this, on
    either version.
    """
    from tests.integration.conftest import profile_for

    profile = profile_for(clickhouse_container, protocol, test_db)

    outcomes = run_clickhouse_checks(profile, deep=True)

    print("\n".join(f"{o.name}: ok={o.ok} detail={o.detail}" for o in outcomes))

    assert all(o.ok for o in outcomes), [o for o in outcomes if not o.ok]
    names = {o.name for o in outcomes}
    assert "deep_replace_from_missing_partition" in names
    detail = next(o.detail for o in outcomes if o.name == "deep_replace_from_missing_partition")
    assert detail  # non-empty — the observed behaviour is recorded for humans to read


# ── staging table in a separate database: REPLACE PARTITION FROM works across
#    databases, and nothing named *__afly_staging is left in the destination db ──


@pytest.mark.integration
def test_rebuild_with_staging_in_a_separate_database(
    manager: ClickHouseManager, test_db: str
) -> None:
    staging_db = f"{test_db}_staging"
    try:
        ensure_destination(manager, test_db, _TABLE)
        rebuilder = PartitionRebuilder(manager, test_db, _TABLE, staging_db=staging_db)
        rebuilder.prepare_staging()

        day = date(2026, 9, 10)
        pid = partition_id(day, "month")
        rows = [
            _row(app_id="app1", day=day, extract="standard", marker="s1"),
            _row(app_id="app1", day=day, extract="standard", marker="s2"),
        ]
        result = rebuilder.rebuild_partition(pid, {(day, "standard", "app1")}, rows)

        assert result.action == "replace"
        assert len(_rows_for_day(manager, test_db, day)) == 2
        assert manager.table_exists(staging_db, rebuilder.staging)
        assert not manager.table_exists(test_db, f"{_TABLE}__afly_staging")

        rebuilder.drop_staging()
        assert not manager.table_exists(staging_db, rebuilder.staging)
    finally:
        manager.execute(f"DROP DATABASE IF EXISTS `{staging_db}`")
