"""Tests for `afly.database.ddl`: DDL text, partition ids, and schema-drift checks."""

from __future__ import annotations

from datetime import date

import pytest

from afly.database.ddl import (
    SchemaMismatchError,
    check_schema,
    destination_ddl,
    ensure_destination,
    partition_id,
    staging_table_name,
)
from afly.schema import DESTINATION_COLUMNS, ORDER_BY, partition_by
from tests.unit.fakes import FakeManager


def _columns_rows() -> list[dict[str, str]]:
    return [{"name": name, "type": ch_type} for name, ch_type in DESTINATION_COLUMNS]


def _matching_partition_key_rows(granularity: str = "month") -> list[dict[str, str]]:
    return [{"partition_key": partition_by(granularity)}]  # type: ignore[arg-type]


@pytest.mark.unit
def test_destination_ddl_has_every_column() -> None:
    sql = destination_ddl("marts", "af_reports")
    for name, ch_type in DESTINATION_COLUMNS:
        assert f"`{name}` {ch_type}" in sql


@pytest.mark.unit
def test_destination_ddl_shape_month_default() -> None:
    sql = destination_ddl("marts", "af_reports")
    assert sql.startswith("CREATE TABLE IF NOT EXISTS `marts`.`af_reports` (")
    assert "ENGINE = MergeTree" in sql
    assert "PARTITION BY toYYYYMM(date)" in sql
    order_by_sql = ", ".join(f"`{c}`" for c in ORDER_BY)
    assert f"ORDER BY ({order_by_sql})" in sql


@pytest.mark.unit
def test_destination_ddl_shape_day_granularity() -> None:
    sql = destination_ddl("marts", "af_reports", "day")
    assert "PARTITION BY toYYYYMMDD(date)" in sql


@pytest.mark.unit
def test_destination_ddl_qualifies_table_name() -> None:
    sql = destination_ddl(
        "some_db", "some.table"
    )  # dotted table name still gets quoted, not parsed
    assert "`some_db`.`some.table`" in sql


@pytest.mark.unit
def test_staging_table_name() -> None:
    assert staging_table_name("af_reports") == "af_reports__afly_staging"


@pytest.mark.unit
def test_partition_id_formats_month_by_default() -> None:
    assert partition_id(date(2026, 9, 1)) == "202609"
    assert partition_id(date(2026, 12, 31)) == "202612"


@pytest.mark.unit
def test_partition_id_formats_yyyymmdd_for_day_granularity() -> None:
    assert partition_id(date(2026, 9, 1), "day") == "20260901"
    assert partition_id(date(2026, 12, 31), "day") == "20261231"


@pytest.mark.unit
def test_check_schema_all_present_is_ok() -> None:
    fake = FakeManager()
    fake.query_dicts_queue = [_columns_rows(), _matching_partition_key_rows()]

    result = check_schema(fake, "marts", "af_reports")

    assert result.ok is True
    assert result.missing == []
    assert result.type_mismatch == []
    assert result.partition_mismatch is None
    assert result.alter_sql is None


@pytest.mark.unit
def test_check_schema_extra_user_columns_are_fine() -> None:
    fake = FakeManager()
    fake.query_dicts_queue = [
        _columns_rows() + [{"name": "my_custom_col", "type": "String"}],
        _matching_partition_key_rows(),
    ]

    result = check_schema(fake, "marts", "af_reports")

    assert result.ok is True
    assert result.missing == []


@pytest.mark.unit
def test_check_schema_missing_column_produces_alter_sql() -> None:
    fake = FakeManager()
    without_roi = [(n, t) for n, t in DESTINATION_COLUMNS if n != "roi"]
    fake.query_dicts_queue = [
        [{"name": n, "type": t} for n, t in without_roi],
        _matching_partition_key_rows(),
    ]

    result = check_schema(fake, "marts", "af_reports")

    assert result.ok is False
    assert ("roi", "Nullable(Float64)") in result.missing
    assert result.alter_sql is not None
    assert "ALTER TABLE `marts`.`af_reports`" in result.alter_sql
    assert "ADD COLUMN `roi` Nullable(Float64)" in result.alter_sql


@pytest.mark.unit
def test_check_schema_type_mismatch_no_alter_suggested() -> None:
    fake = FakeManager()
    changed = [{"name": n, "type": "String" if n == "roi" else t} for n, t in DESTINATION_COLUMNS]
    fake.query_dicts_queue = [changed, _matching_partition_key_rows()]

    result = check_schema(fake, "marts", "af_reports")

    assert result.ok is False
    assert result.missing == []
    assert result.type_mismatch == [("roi", "Nullable(Float64)", "String")]
    # No safe auto-fix for a type change, so no ALTER is proposed for it.
    assert result.alter_sql is None


@pytest.mark.unit
def test_check_schema_whitespace_insensitive() -> None:
    fake = FakeManager()
    # Same types, just re-spaced — must not be reported as a mismatch.
    fake.query_dicts_queue = [
        [{"name": n, "type": t.replace(", ", ",")} for n, t in DESTINATION_COLUMNS],
        [{"partition_key": partition_by("month").replace("(", " (")}],
    ]

    result = check_schema(fake, "marts", "af_reports")

    assert result.ok is True


@pytest.mark.unit
def test_check_schema_partition_key_mismatch() -> None:
    fake = FakeManager()
    fake.query_dicts_queue = [_columns_rows(), [{"partition_key": "toYYYYMMDD(date)"}]]

    result = check_schema(fake, "marts", "af_reports", "month")

    assert result.ok is False
    assert result.missing == []
    assert result.type_mismatch == []
    assert result.partition_mismatch == "toYYYYMMDD(date)"


@pytest.mark.unit
def test_check_schema_no_partition_key_row_is_not_flagged() -> None:
    """A table system.tables can't find (edge case, shouldn't happen once
    table_exists() is true) never produces a false partition mismatch."""
    fake = FakeManager()
    fake.query_dicts_queue = [_columns_rows(), []]

    result = check_schema(fake, "marts", "af_reports")

    assert result.ok is True
    assert result.partition_mismatch is None


@pytest.mark.unit
def test_ensure_destination_creates_when_missing() -> None:
    fake = FakeManager()
    fake.table_exists_result = False

    created = ensure_destination(fake, "marts", "af_reports")

    assert created is True
    assert fake.executed  # the CREATE TABLE statement was issued
    assert "CREATE TABLE IF NOT EXISTS `marts`.`af_reports`" in fake.executed[0][0]
    assert "PARTITION BY toYYYYMM(date)" in fake.executed[0][0]


@pytest.mark.unit
def test_ensure_destination_creates_with_day_granularity_when_missing() -> None:
    fake = FakeManager()
    fake.table_exists_result = False

    ensure_destination(fake, "marts", "af_reports", "day")

    assert "PARTITION BY toYYYYMMDD(date)" in fake.executed[0][0]


@pytest.mark.unit
def test_ensure_destination_ok_when_matching() -> None:
    fake = FakeManager()
    fake.table_exists_result = True
    fake.query_dicts_queue = [_columns_rows(), _matching_partition_key_rows()]

    created = ensure_destination(fake, "marts", "af_reports")

    assert created is False


@pytest.mark.unit
def test_ensure_destination_raises_on_missing_columns() -> None:
    fake = FakeManager()
    fake.table_exists_result = True
    without_roi = [(n, t) for n, t in DESTINATION_COLUMNS if n != "roi"]
    fake.query_dicts_queue = [
        [{"name": n, "type": t} for n, t in without_roi],
        _matching_partition_key_rows(),
    ]

    with pytest.raises(SchemaMismatchError) as excinfo:
        ensure_destination(fake, "marts", "af_reports")

    assert "roi" in str(excinfo.value)
    assert "afly never ALTERs" in str(excinfo.value)


@pytest.mark.unit
def test_ensure_destination_raises_on_partition_key_mismatch() -> None:
    fake = FakeManager()
    fake.table_exists_result = True
    fake.query_dicts_queue = [_columns_rows(), [{"partition_key": "toYYYYMMDD(date)"}]]

    with pytest.raises(SchemaMismatchError) as excinfo:
        ensure_destination(fake, "marts", "af_reports", "month")

    message = str(excinfo.value)
    assert "partition key mismatch" in message
    assert "toYYYYMMDD(date)" in message
    assert "toYYYYMM(date)" in message
