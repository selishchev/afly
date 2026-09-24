"""The single source of truth for afly's ClickHouse destination column list.

Every extract (whatever its ``report_type``/``category``) is normalized into
rows shaped exactly like :data:`DESTINATION_COLUMNS` before they reach
ClickHouse — ``afly.csvmap.parser`` builds rows against this list, and M3's
DDL generator (``CREATE TABLE`` for a fresh destination) reads it too. Having
one module both sides import means the parser and the table schema can never
silently drift apart.

Column order matters: it is the order rows are keyed/serialized in, and the
order M3 will render ``CREATE TABLE`` columns in.
"""

from __future__ import annotations

from typing import Literal

# The two supported destination-partition granularities. "month" is the
# project default (see ExtractDefaults.partition_granularity) — a real
# destination table has been measured at ~1.7MB/41k rows per 23 days, so a
# daily partition (the pre-configurable-granularity behaviour) means hundreds
# of tiny parts a year; ClickHouse's own guidance is coarse (monthly)
# partitions. "day" is kept for projects that need it (e.g. a very high
# per-day row count, where a whole month in one partition would make a
# REPLACE PARTITION rewrite too large).
Granularity = Literal["month", "day"]

DEFAULT_PARTITION_GRANULARITY: Granularity = "month"

# PARTITION BY expression per granularity — the single source of truth both
# afly.database.ddl (CREATE TABLE / partition id computation) and afly.
# database.writer (the WHERE clause a rebuild scopes itself to) key off of.
PARTITION_BY_EXPR: dict[Granularity, str] = {
    "month": "toYYYYMM(date)",
    "day": "toYYYYMMDD(date)",
}


def partition_by(granularity: Granularity) -> str:
    """The ``PARTITION BY`` expression for *granularity*."""
    return PARTITION_BY_EXPR[granularity]


DESTINATION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("app_id", "LowCardinality(String)"),
    ("date", "Date"),
    ("report_type", "LowCardinality(String)"),
    ("category", "LowCardinality(String)"),
    ("is_retargeting", "UInt8"),
    ("agency", "String"),
    ("media_source", "LowCardinality(String)"),
    ("campaign", "String"),
    ("campaign_name", "String"),
    ("campaign_id", "String"),
    ("adset", "String"),
    ("adset_id", "String"),
    ("adgroup", "String"),
    ("adgroup_id", "String"),
    ("country", "LowCardinality(String)"),
    ("currency", "LowCardinality(String)"),
    ("impressions", "Nullable(UInt64)"),
    ("clicks", "Nullable(UInt64)"),
    ("ctr", "Nullable(Float64)"),
    ("installs", "Nullable(UInt64)"),
    ("conversion_rate", "Nullable(Float64)"),
    ("sessions", "Nullable(UInt64)"),
    ("loyal_users", "Nullable(UInt64)"),
    ("loyal_users_rate", "Nullable(Float64)"),
    ("total_revenue", "Nullable(Float64)"),
    ("total_cost", "Nullable(Float64)"),
    ("roi", "Nullable(Float64)"),
    ("arpu", "Nullable(Float64)"),
    ("average_ecpi", "Nullable(Float64)"),
    ("event_unique_users", "Map(String, UInt64)"),
    ("event_counter", "Map(String, UInt64)"),
    ("event_sales", "Map(String, Float64)"),
    ("extra", "Map(String, String)"),
    ("_extract", "LowCardinality(String)"),
    ("_run_id", "String"),
    ("_loaded_at", "DateTime64(3, 'UTC')"),
)

COLUMN_NAMES: tuple[str, ...] = tuple(name for name, _ in DESTINATION_COLUMNS)

# Key columns for the destination MergeTree's ORDER BY / PARTITION BY — the
# grain a row is idempotently identified by (M3's DDL and delete-then-insert
# write path both key on this).
ORDER_BY: tuple[str, ...] = (
    "app_id",
    "date",
    "media_source",
    "campaign",
    "adset_id",
    "adgroup_id",
    "country",
)

# Columns stamped from ReportContext (afly.csvmap.parser) rather than parsed
# out of the CSV body — listed here so the parser and its tests have one
# place to check "is this column context or content".
CONTEXT_COLUMNS: tuple[str, ...] = (
    "app_id",
    "report_type",
    "category",
    "is_retargeting",
    "_extract",
    "_run_id",
    "_loaded_at",
)
