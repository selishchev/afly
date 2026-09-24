"""Tests for `afly.database.writer.PartitionRebuilder` — the partition-rebuild write path.

Uses `FakeManager` (a recording spy, see tests/unit/fakes.py) to verify the
*sequence* of statements and their shape without a real server — genuine
REPLACE/DROP PARTITION behaviour is covered end-to-end in
tests/integration/test_clickhouse_layer.py.
"""

from __future__ import annotations

from datetime import date

import pytest

from afly.database.writer import PartitionRebuilder
from tests.unit.fakes import FakeManager


@pytest.mark.unit
def test_staging_table_name() -> None:
    rebuilder = PartitionRebuilder(FakeManager(), "marts", "af_reports")
    assert rebuilder.staging == "af_reports__afly_staging"


@pytest.mark.unit
def test_granularity_defaults_to_month() -> None:
    rebuilder = PartitionRebuilder(FakeManager(), "marts", "af_reports")
    assert rebuilder.granularity == "month"


@pytest.mark.unit
def test_granularity_can_be_set_to_day() -> None:
    rebuilder = PartitionRebuilder(FakeManager(), "marts", "af_reports", "day")
    assert rebuilder.granularity == "day"


@pytest.mark.unit
def test_prepare_staging_drops_then_creates_as() -> None:
    fake = FakeManager()
    rebuilder = PartitionRebuilder(fake, "marts", "af_reports")

    rebuilder.prepare_staging()

    assert fake.call_names() == ["drop_table", "create_table_as"]
    assert fake.calls[0][1] == ("marts", "af_reports__afly_staging")
    assert fake.calls[1][1] == ("marts", "af_reports__afly_staging", "marts", "af_reports")


@pytest.mark.unit
def test_drop_staging() -> None:
    fake = FakeManager()
    rebuilder = PartitionRebuilder(fake, "marts", "af_reports")

    rebuilder.drop_staging()

    assert fake.call_names() == ["drop_table"]


@pytest.mark.unit
def test_rebuild_partition_statement_order_replace() -> None:
    fake = FakeManager()
    # count() runs AFTER both inserts (matches the spec's literal statement
    # order), so this is the staging TOTAL: 2 kept + 1 fresh row below.
    fake.count_result = 3
    rebuilder = PartitionRebuilder(fake, "marts", "af_reports", "day")

    result = rebuilder.rebuild_partition("20260910", coverage=set(), fresh_rows=[{"app_id": "a1"}])

    assert fake.call_names() == [
        "truncate",
        "execute",
        "insert_rows",
        "count",
        "execute",
        "truncate",
    ]
    assert result.action == "replace"
    assert result.kept_rows == 2  # total(3) - fresh(1)
    assert result.fresh_rows == 1
    assert result.partition_id == "20260910"
    # First and last calls are both truncate(staging).
    assert fake.calls[0][1] == ("marts", "af_reports__afly_staging")
    assert fake.calls[-1][1] == ("marts", "af_reports__afly_staging")
    # The REPLACE statement targets the destination, sourced from staging.
    replace_sql = fake.executed[-1][0]
    assert "ALTER TABLE `marts`.`af_reports`" in replace_sql
    assert "REPLACE PARTITION ID '20260910'" in replace_sql
    assert "FROM `marts`.`af_reports__afly_staging`" in replace_sql


@pytest.mark.unit
def test_rebuild_partition_uses_month_partition_expression_by_default() -> None:
    fake = FakeManager()
    fake.count_result = 0
    rebuilder = PartitionRebuilder(fake, "marts", "af_reports")  # default: month

    rebuilder.rebuild_partition("202609", coverage=set(), fresh_rows=[])

    insert_sql, insert_params = fake.executed[0]
    assert "toYYYYMM(date) = %(pid)s" in insert_sql
    assert insert_params is not None
    assert insert_params["pid"] == 202609


@pytest.mark.unit
def test_rebuild_partition_uses_day_partition_expression_when_configured() -> None:
    fake = FakeManager()
    fake.count_result = 0
    rebuilder = PartitionRebuilder(fake, "marts", "af_reports", "day")

    rebuilder.rebuild_partition("20260910", coverage=set(), fresh_rows=[])

    insert_sql, insert_params = fake.executed[0]
    assert "toYYYYMMDD(date) = %(pid)s" in insert_sql
    assert insert_params is not None
    assert insert_params["pid"] == 20260910


@pytest.mark.unit
def test_rebuild_partition_drop_when_staging_ends_up_empty_and_partition_exists() -> None:
    fake = FakeManager()
    fake.count_result = 0
    fake.partition_exists_result = True
    rebuilder = PartitionRebuilder(fake, "marts", "af_reports", "day")

    result = rebuilder.rebuild_partition("20260910", coverage=set(), fresh_rows=[])

    assert result.action == "drop"
    assert result.kept_rows == 0
    assert result.fresh_rows == 0
    drop_sql = fake.executed[-1][0]
    assert "DROP PARTITION ID '20260910'" in drop_sql
    # No insert_rows call — fresh_rows was empty.
    assert fake.call_names() == [
        "truncate",
        "execute",
        "count",
        "partition_exists",
        "execute",
        "truncate",
    ]


@pytest.mark.unit
def test_rebuild_partition_noop_when_staging_empty_and_no_partition_on_dest() -> None:
    fake = FakeManager()
    fake.count_result = 0
    fake.partition_exists_result = False
    rebuilder = PartitionRebuilder(fake, "marts", "af_reports", "day")

    result = rebuilder.rebuild_partition("20260910", coverage=set(), fresh_rows=[])

    assert result.action == "noop"
    # Only the kept-rows SELECT-insert was executed via execute() — no REPLACE/DROP.
    assert len(fake.executed) == 1
    assert fake.executed[0][0].startswith("INSERT INTO")
    assert fake.call_names() == ["truncate", "execute", "count", "partition_exists", "truncate"]


@pytest.mark.unit
def test_rebuild_partition_includes_not_in_triples_when_coverage_given() -> None:
    fake = FakeManager()
    fake.count_result = 0
    fake.partition_exists_result = False
    rebuilder = PartitionRebuilder(fake, "marts", "af_reports", "day")

    coverage = {
        (date(2026, 9, 10), "facebook", "app1"),
        (date(2026, 9, 10), "standard", "app2"),
    }
    rebuilder.rebuild_partition("20260910", coverage=coverage, fresh_rows=[])

    insert_sql, insert_params = fake.executed[0]
    assert "NOT IN %(triples)s" in insert_sql
    assert insert_params is not None
    assert set(insert_params["triples"]) == coverage
    assert insert_params["pid"] == 20260910


@pytest.mark.unit
def test_rebuild_partition_omits_not_in_predicate_when_coverage_empty() -> None:
    fake = FakeManager()
    fake.count_result = 5
    rebuilder = PartitionRebuilder(fake, "marts", "af_reports", "day")

    rebuilder.rebuild_partition("20260910", coverage=set(), fresh_rows=[])

    insert_sql, insert_params = fake.executed[0]
    assert "NOT IN" not in insert_sql
    assert insert_params is not None
    assert "triples" not in insert_params


@pytest.mark.unit
def test_rebuild_partition_truncates_staging_in_finally_even_on_error() -> None:
    fake = FakeManager()
    fake.raise_on_execute = RuntimeError("boom")
    rebuilder = PartitionRebuilder(fake, "marts", "af_reports", "day")

    with pytest.raises(RuntimeError, match="boom"):
        rebuilder.rebuild_partition("20260910", coverage=set(), fresh_rows=[])

    # truncate must still have run both before the failing statement and in `finally`.
    assert fake.call_names().count("truncate") == 2
    assert fake.call_names()[0] == "truncate"
    assert fake.call_names()[-1] == "truncate"


@pytest.mark.unit
def test_rebuild_partition_inserts_duplicate_fresh_rows_both() -> None:
    """AppsFlyer can return two data rows identical on every dimension within
    one pull — MergeTree has no dedup, and this is a product requirement
    (both must count). Guards against a future dedup-by-accident (e.g.
    routing fresh_rows through a set/dict keyed by content)."""
    fake = FakeManager()
    fake.count_result = 2
    rebuilder = PartitionRebuilder(fake, "marts", "af_reports", "day")
    identical_row = {"app_id": "a1", "installs": 5}

    result = rebuilder.rebuild_partition(
        "20260910", coverage=set(), fresh_rows=[dict(identical_row), dict(identical_row)]
    )

    assert result.fresh_rows == 2
    inserted_db, inserted_table, inserted_columns, inserted_rows = fake.inserted[0]
    assert len(inserted_rows) == 2
    assert inserted_rows[0] == inserted_rows[1] == identical_row


@pytest.mark.unit
def test_rows_for_pair_filters_on_exact_day_regardless_of_granularity() -> None:
    fake = FakeManager()
    fake.count_result = 42
    rebuilder = PartitionRebuilder(fake, "marts", "af_reports")  # default: month

    n = rebuilder.rows_for_pair(date(2026, 9, 10), "standard", "app1")

    assert n == 42
    name, args, kwargs = fake.calls[0]
    assert name == "count"
    assert args[:2] == ("marts", "af_reports")
    where, params = args[2], args[3]
    assert "date = %(day)s" in where
    assert params == {"day": date(2026, 9, 10), "extract": "standard", "app_id": "app1"}


@pytest.mark.unit
def test_staging_in_a_separate_database_is_qualified_and_created() -> None:
    fake = FakeManager()
    rebuilder = PartitionRebuilder(fake, "raw", "af_reports", staging_db="afly")

    assert rebuilder.staging_db == "afly"
    # destination db is part of the name so two destinations never share it
    assert rebuilder.staging == "raw__af_reports__afly_staging"

    rebuilder.prepare_staging()

    assert fake.call_names() == ["create_database", "drop_table", "create_table_as"]
    assert fake.calls[1][1] == ("afly", "raw__af_reports__afly_staging")
    assert fake.calls[2][1] == ("afly", "raw__af_reports__afly_staging", "raw", "af_reports")


@pytest.mark.unit
def test_staging_db_equal_to_destination_keeps_the_plain_name() -> None:
    rebuilder = PartitionRebuilder(FakeManager(), "raw", "af_reports", staging_db="raw")
    assert rebuilder.staging == "af_reports__afly_staging"
