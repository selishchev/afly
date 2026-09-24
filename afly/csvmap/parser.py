"""Parse an AppsFlyer aggregate Pull API v5 CSV body into destination rows.

The single place that knows the CSV↔schema mapping end to end: every output
row has exactly ``afly.schema.COLUMN_NAMES`` as keys, in that order, so
downstream (M3's ClickHouse writer) never has to know anything about CSV
headers, event triples, or AppsFlyer's null/format quirks — those are all
resolved here via ``csvmap.headers``/``csvmap.values``.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from afly import schema
from afly.csvmap.currency import resolve_currency
from afly.csvmap.headers import (
    EVENT_KIND_SALES,
    EVENT_KIND_TO_COLUMN,
    classify_header,
    detect_format,
)
from afly.csvmap.values import to_date, to_float, to_str, to_uint

# Which schema columns are read via to_uint()/to_float(); everything else
# among the "column"-classified headers is a to_str() dimension.
_UINT_COLUMNS = frozenset({"impressions", "clicks", "installs", "sessions", "loyal_users"})
_FLOAT_COLUMNS = frozenset(
    {
        "ctr",
        "conversion_rate",
        "loyal_users_rate",
        "total_revenue",
        "total_cost",
        "roi",
        "arpu",
        "average_ecpi",
    }
)


@dataclass(frozen=True)
class ReportContext:
    """Everything a report body needs stamped onto every row, but that isn't
    itself part of the CSV (it's what was *asked for*, not what came back).

    ``currency`` is the app's own currency, known ahead of the pull from the
    AppsFlyer app-list API (``afly.appsflyer.mng_api.AppInfo.currency``) —
    when the caller has it, pass it here. It is only a *hint*: AppsFlyer
    ignores the `currency=USD` query param on these aggregate reports and
    always returns money in the app's own currency, so the header-detected
    currency (see :func:`parse_report`) is the ground truth whenever the two
    disagree.
    """

    app_id: str
    report_type: str
    category: str
    is_retargeting: bool
    extract: str
    run_id: str
    loaded_at: datetime
    currency: str | None = None


@dataclass
class ParsedReport:
    """The parsed rows plus enough metadata to log/debug the parse."""

    rows: list[dict[str, Any]]
    headers: list[str]
    format: str
    currency: str
    unknown_headers: list[str]
    warnings: list[str]
    dropped_out_of_range: int
    dropped_excluded: int


def parse_report(
    text: str,
    ctx: ReportContext,
    *,
    date_range: tuple[date, date] | None = None,
    exclude_media_sources: Iterable[str] = (),
    keep_unknown_columns: bool = False,
) -> ParsedReport:
    """Parse *text* (an already-decoded CSV body) into destination-shaped rows.

    Raises :class:`ValueError` (naming the offending row number) if a row's
    ``Date`` cell can't be parsed — every other cell-level problem degrades
    to a null rather than failing the whole report, since a single bad
    metric shouldn't sink an otherwise-good day's data.
    """
    reader = csv.DictReader(io.StringIO(text))
    headers = list(reader.fieldnames or [])
    fmt = detect_format(headers)
    classified = {h: classify_header(h) for h in headers}

    unknown_headers = [h for h, c in classified.items() if c.kind == "unknown"]
    warnings = [f"unknown CSV headers: {', '.join(unknown_headers)}"] if unknown_headers else []

    currency, currency_warnings = resolve_currency(ctx.currency, classified)
    warnings.extend(currency_warnings)

    has_campaign_c = any(c.kind == "column" and c.name == "campaign" for c in classified.values())
    has_campaign_name = any(
        c.kind == "column" and c.name == "campaign_name" for c in classified.values()
    )
    exclude_set = set(exclude_media_sources)

    rows: list[dict[str, Any]] = []
    dropped_out_of_range = 0
    dropped_excluded = 0

    for line_no, raw_row in enumerate(reader, start=2):  # header is line 1
        row = _parse_row(raw_row, classified, line_no, keep_unknown_columns)

        if not has_campaign_c and has_campaign_name:
            row["campaign"] = row["campaign_name"]

        _stamp_context(row, ctx, currency)

        if date_range is not None and isinstance(row["date"], date):
            start, end = date_range
            if not (start <= row["date"] <= end):
                dropped_out_of_range += 1
                continue
        if row["media_source"] in exclude_set:
            dropped_excluded += 1
            continue

        rows.append({name: row[name] for name in schema.COLUMN_NAMES})

    return ParsedReport(
        rows=rows,
        headers=headers,
        format=fmt,
        currency=currency,
        unknown_headers=unknown_headers,
        warnings=warnings,
        dropped_out_of_range=dropped_out_of_range,
        dropped_excluded=dropped_excluded,
    )


def _default_row() -> dict[str, Any]:
    row: dict[str, Any] = dict.fromkeys(schema.COLUMN_NAMES, "")
    for name in _UINT_COLUMNS | _FLOAT_COLUMNS:
        row[name] = None
    row["is_retargeting"] = 0
    row["event_unique_users"] = {}
    row["event_counter"] = {}
    row["event_sales"] = {}
    row["extra"] = {}
    return row


def _parse_row(
    raw_row: dict[str | None, str],
    classified: dict[str, Any],
    line_no: int,
    keep_unknown_columns: bool,
) -> dict[str, Any]:
    row = _default_row()

    for header, raw_value in raw_row.items():
        if header is None:
            continue  # csv.DictReader's catch-all for a ragged extra field
        c = classified[header]

        if c.kind == "column":
            column = c.name
            assert column is not None
            if column == "date":
                try:
                    row["date"] = to_date(raw_value or "")
                except ValueError as exc:
                    raise ValueError(f"row {line_no}: {exc}") from exc
            elif column in _UINT_COLUMNS:
                row[column] = to_uint(raw_value)
            elif column in _FLOAT_COLUMNS:
                row[column] = to_float(raw_value)
            else:
                row[column] = to_str(raw_value)
        elif c.kind == "event":
            event_name = c.event_name or ""
            dest_map = row[EVENT_KIND_TO_COLUMN[c.name]]
            value: int | float | None = (
                to_float(raw_value) if c.name == EVENT_KIND_SALES else to_uint(raw_value)
            )
            if value is not None:
                dest_map[event_name] = value
        elif keep_unknown_columns:
            row["extra"][header] = to_str(raw_value)

    return row


def _stamp_context(row: dict[str, Any], ctx: ReportContext, currency: str) -> None:
    row["app_id"] = ctx.app_id
    row["report_type"] = ctx.report_type
    row["category"] = ctx.category
    row["is_retargeting"] = 1 if ctx.is_retargeting else 0
    row["currency"] = currency
    row["_extract"] = ctx.extract
    row["_run_id"] = ctx.run_id
    row["_loaded_at"] = ctx.loaded_at
