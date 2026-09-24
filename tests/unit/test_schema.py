"""Sanity checks on the destination column list — the contract M3's DDL and
the CSV parser both build against."""

from __future__ import annotations

import re

import pytest

from afly import schema


@pytest.mark.unit
def test_column_names_unique() -> None:
    assert len(schema.COLUMN_NAMES) == len(set(schema.COLUMN_NAMES))


@pytest.mark.unit
def test_column_names_matches_destination_columns_order() -> None:
    assert schema.COLUMN_NAMES == tuple(name for name, _ in schema.DESTINATION_COLUMNS)


@pytest.mark.unit
def test_order_by_is_subset_of_column_names() -> None:
    assert set(schema.ORDER_BY) <= set(schema.COLUMN_NAMES)


@pytest.mark.unit
def test_order_by_columns_are_not_nullable() -> None:
    types_by_name = dict(schema.DESTINATION_COLUMNS)
    for name in schema.ORDER_BY:
        ch_type = types_by_name[name]
        assert not ch_type.startswith("Nullable("), f"{name} is Nullable but used in ORDER_BY"


@pytest.mark.unit
def test_context_columns_are_subset_of_column_names() -> None:
    assert set(schema.CONTEXT_COLUMNS) <= set(schema.COLUMN_NAMES)


@pytest.mark.unit
def test_partition_by_is_a_nonempty_expression_for_every_granularity() -> None:
    for granularity in ("month", "day"):
        expr = schema.partition_by(granularity)  # type: ignore[arg-type]
        assert expr
        assert "date" in expr.lower()


@pytest.mark.unit
def test_partition_by_month_and_day_differ() -> None:
    assert schema.partition_by("month") == "toYYYYMM(date)"
    assert schema.partition_by("day") == "toYYYYMMDD(date)"


@pytest.mark.unit
def test_default_partition_granularity_is_month() -> None:
    assert schema.DEFAULT_PARTITION_GRANULARITY == "month"


@pytest.mark.unit
def test_every_type_is_a_recognizable_clickhouse_type() -> None:
    # Loose sanity check, not a full grammar: every declared type is one of
    # the shapes afly actually uses (LowCardinality/Nullable/Map wrappers,
    # or a bare scalar type) — catches an obvious typo without hardcoding
    # the exact list of ClickHouse types.
    pattern = re.compile(
        r"^(LowCardinality\(\w+\)|Nullable\(\w+\)|Map\(\w+,\s*\w+\)|String|UInt8|"
        r"Date|DateTime64\(\d+,\s*'[A-Za-z/]+'\))$"
    )
    for name, ch_type in schema.DESTINATION_COLUMNS:
        assert pattern.match(ch_type), f"{name}: unrecognized type {ch_type!r}"
