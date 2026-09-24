"""Unit tests for afly.csvmap.parser against the tests/fixtures/csv/* files.

Each fixture exercises a specific corner of the AppsFlyer CSV format (see
the milestone docstring / afly/schema.py); tests here confirm parse_report
handles every one of them and that every output row is shaped exactly like
afly.schema.COLUMN_NAMES.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from afly import schema
from afly.csvmap.parser import ReportContext, parse_report

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "csv"


def _read(name: str) -> str:
    # utf-8-sig mirrors what afly.appsflyer.pull_api.fetch_report does with
    # the real HTTP body — strips a BOM if present, no-ops otherwise.
    return (_FIXTURES_DIR / name).read_text(encoding="utf-8-sig")


def _ctx(**overrides: object) -> ReportContext:
    defaults: dict[str, object] = {
        "app_id": "com.test.app",
        "report_type": "geo_by_date_report",
        "category": "standard",
        "is_retargeting": False,
        "extract": "my_extract",
        "run_id": "20260922T000000Z-abc123",
        "loaded_at": datetime(2026, 9, 22, 0, 0, 0),
    }
    defaults.update(overrides)
    return ReportContext(**defaults)  # type: ignore[arg-type]


@pytest.mark.unit
def test_every_row_has_exactly_schema_columns_in_order() -> None:
    parsed = parse_report(_read("standard.csv"), _ctx())
    assert parsed.rows  # sanity: fixture actually has rows
    for row in parsed.rows:
        assert list(row.keys()) == list(schema.COLUMN_NAMES)


@pytest.mark.unit
def test_standard_format_detected() -> None:
    parsed = parse_report(_read("standard.csv"), _ctx())
    assert parsed.format == "standard"
    assert parsed.unknown_headers == []


@pytest.mark.unit
def test_standard_row_count() -> None:
    parsed = parse_report(_read("standard.csv"), _ctx())
    assert len(parsed.rows) == 4


@pytest.mark.unit
def test_standard_googleadwords_row_values() -> None:
    parsed = parse_report(_read("standard.csv"), _ctx())
    row = next(r for r in parsed.rows if r["media_source"] == "googleadwords_int")

    assert row["date"] == date(2026, 9, 1)
    assert row["country"] == "US"
    assert row["campaign"] == "Campaign A"
    assert row["impressions"] == 10000
    assert row["clicks"] == 500
    assert row["ctr"] == pytest.approx(5.0)
    assert row["installs"] == 50
    assert row["total_revenue"] == pytest.approx(1000.5)
    assert row["total_cost"] == pytest.approx(200.25)
    assert row["currency"] == "USD"  # detected from the "(Sales in USD)" header
    assert row["event_unique_users"] == {"af_purchase": 15}
    assert row["event_counter"] == {"af_purchase": 18}
    assert row["event_sales"] == {"af_purchase": pytest.approx(300.75)}


@pytest.mark.unit
def test_standard_organic_row_nulls_and_none_campaign() -> None:
    parsed = parse_report(_read("standard.csv"), _ctx())
    row = next(r for r in parsed.rows if r["country"] == "DE")

    assert row["campaign"] == "None"  # AppsFlyer's literal "None" is kept for dimensions
    assert row["impressions"] is None
    assert row["clicks"] is None
    assert row["ctr"] is None
    assert row["installs"] == 100
    # no event triple values in this row -> empty maps, not missing keys
    assert row["event_unique_users"] == {}
    assert row["event_counter"] == {}
    assert row["event_sales"] == {}


@pytest.mark.unit
def test_standard_yandex_row_agency_and_na_nulls() -> None:
    parsed = parse_report(_read("standard.csv"), _ctx())
    row = next(r for r in parsed.rows if r["media_source"] == "yandexdirect_int")

    assert row["agency"] == "agency_x"
    assert row["total_cost"] is None  # N/A
    assert row["roi"] is None  # N/A
    assert row["average_ecpi"] is None  # N/A
    assert row["total_revenue"] == pytest.approx(150.0)


@pytest.mark.unit
def test_standard_facebook_ads_aggregated_row() -> None:
    parsed = parse_report(_read("standard.csv"), _ctx())
    row = next(r for r in parsed.rows if r["media_source"] == "Facebook Ads")

    assert row["country"] == "US"
    assert row["campaign"] == "Campaign C"
    assert row["total_revenue"] == pytest.approx(2000.0)


@pytest.mark.unit
def test_standard_context_columns_stamped() -> None:
    ctx = _ctx(
        app_id="com.example.app",
        report_type="daily_report",
        category="facebook",
        is_retargeting=True,
    )
    parsed = parse_report(_read("standard.csv"), ctx)
    row = parsed.rows[0]

    assert row["app_id"] == "com.example.app"
    assert row["report_type"] == "daily_report"
    assert row["category"] == "facebook"
    assert row["is_retargeting"] == 1
    assert row["_extract"] == "my_extract"
    assert row["_run_id"] == "20260922T000000Z-abc123"
    assert row["_loaded_at"] == datetime(2026, 9, 22, 0, 0, 0)


@pytest.mark.unit
def test_standard_is_retargeting_false_stamps_zero() -> None:
    parsed = parse_report(_read("standard.csv"), _ctx(is_retargeting=False))
    assert all(row["is_retargeting"] == 0 for row in parsed.rows)


# --- facebook format -------------------------------------------------------


@pytest.mark.unit
def test_facebook_format_detected() -> None:
    parsed = parse_report(_read("facebook.csv"), _ctx())
    assert parsed.format == "facebook"


@pytest.mark.unit
def test_facebook_campaign_c_used_when_present() -> None:
    parsed = parse_report(_read("facebook.csv"), _ctx())
    assert all(row["campaign"] == "fb_campaign_1" for row in parsed.rows)


@pytest.mark.unit
def test_facebook_adset_fields_differ_between_rows() -> None:
    parsed = parse_report(_read("facebook.csv"), _ctx())
    adset_ids = {row["adset_id"] for row in parsed.rows}
    assert adset_ids == {"1001", "1002"}
    adset_names = {row["adset"] for row in parsed.rows}
    assert adset_names == {"Adset A", "Adset B"}


@pytest.mark.unit
def test_facebook_campaign_name_and_id_populated() -> None:
    parsed = parse_report(_read("facebook.csv"), _ctx())
    for row in parsed.rows:
        assert row["campaign_name"] == "FB Campaign One"
        assert row["campaign_id"] == "c1001"


@pytest.mark.unit
def test_facebook_no_campaign_c_falls_back_to_campaign_name() -> None:
    parsed = parse_report(_read("facebook_no_campaign_c.csv"), _ctx())
    assert parsed.format == "facebook"
    assert all(row["campaign"] == "FB Campaign One" for row in parsed.rows)
    # confirms Campaign Name was in fact the source, not a coincidence
    assert all(row["campaign"] == row["campaign_name"] for row in parsed.rows)


# --- header_only / daily_report / unknown_column ---------------------------


@pytest.mark.unit
def test_header_only_produces_zero_rows_no_error() -> None:
    parsed = parse_report(_read("header_only.csv"), _ctx())
    assert parsed.rows == []
    assert parsed.dropped_out_of_range == 0
    assert parsed.dropped_excluded == 0


@pytest.mark.unit
def test_daily_report_missing_country_and_campaign_default_empty() -> None:
    parsed = parse_report(_read("daily_report.csv"), _ctx(report_type="daily_report"))
    assert len(parsed.rows) == 2
    for row in parsed.rows:
        assert row["country"] == ""
        assert row["campaign"] == ""
        assert row["total_revenue"] is None  # no Total Revenue column at all
        assert row["roi"] is None
        assert row["arpu"] is None


@pytest.mark.unit
def test_daily_report_present_columns_still_parsed() -> None:
    parsed = parse_report(_read("daily_report.csv"), _ctx(report_type="daily_report"))
    row = next(r for r in parsed.rows if r["media_source"] == "googleadwords_int")
    assert row["impressions"] == 3000
    assert row["total_cost"] == pytest.approx(45.0)
    assert row["average_ecpi"] == pytest.approx(3.0)


@pytest.mark.unit
def test_unknown_column_flagged_and_dropped_by_default() -> None:
    parsed = parse_report(_read("unknown_column.csv"), _ctx())
    assert parsed.unknown_headers == ["Mystery Metric"]
    assert any("Mystery Metric" in w for w in parsed.warnings)
    assert parsed.rows[0]["extra"] == {}  # keep_unknown_columns=False by default


@pytest.mark.unit
def test_unknown_column_kept_in_extra_when_enabled() -> None:
    parsed = parse_report(_read("unknown_column.csv"), _ctx(), keep_unknown_columns=True)
    assert parsed.rows[0]["extra"] == {"Mystery Metric": "42"}


# --- BOM / percent / thousands ---------------------------------------------


@pytest.mark.unit
def test_bom_percent_thousands_parses_cleanly() -> None:
    parsed = parse_report(_read("bom_percent_thousands.csv"), _ctx())
    assert len(parsed.rows) == 1
    row = parsed.rows[0]
    # the leading header shouldn't retain a BOM character (would otherwise
    # corrupt the first column name and break "Date" classification)
    assert parsed.headers[0] == "Date"
    assert row["impressions"] == 12000
    assert row["ctr"] == pytest.approx(2.83)


# --- date_range / exclude_media_sources filtering ---------------------------


@pytest.mark.unit
def test_date_range_drops_out_of_range_rows() -> None:
    parsed = parse_report(
        _read("standard.csv"), _ctx(), date_range=(date(2026, 9, 1), date(2026, 9, 1))
    )
    assert all(row["date"] == date(2026, 9, 1) for row in parsed.rows)
    assert parsed.dropped_out_of_range == 2  # the two 2026-09-02 rows
    assert len(parsed.rows) == 2


@pytest.mark.unit
def test_date_range_keeps_everything_when_none() -> None:
    parsed = parse_report(_read("standard.csv"), _ctx(), date_range=None)
    assert parsed.dropped_out_of_range == 0
    assert len(parsed.rows) == 4


@pytest.mark.unit
def test_exclude_media_sources_drops_matching_rows() -> None:
    parsed = parse_report(_read("standard.csv"), _ctx(), exclude_media_sources=["Organic"])
    assert parsed.dropped_excluded == 1
    assert all(row["media_source"] != "Organic" for row in parsed.rows)
    assert len(parsed.rows) == 3


@pytest.mark.unit
def test_exclude_media_sources_and_date_range_combine() -> None:
    parsed = parse_report(
        _read("standard.csv"),
        _ctx(),
        date_range=(date(2026, 9, 2), date(2026, 9, 2)),
        exclude_media_sources=["Facebook Ads"],
    )
    assert parsed.dropped_out_of_range == 2
    assert parsed.dropped_excluded == 1
    assert len(parsed.rows) == 1
    assert parsed.rows[0]["media_source"] == "yandexdirect_int"


# --- unparseable date -------------------------------------------------------


@pytest.mark.unit
def test_unparseable_date_raises_with_row_number() -> None:
    text = "Date,Country\nnot-a-date,US\n"
    with pytest.raises(ValueError, match="row 2"):
        parse_report(text, _ctx())


# --- currency (multi-currency support) --------------------------------------


@pytest.mark.unit
def test_eur_app_sales_columns_land_in_event_sales() -> None:
    parsed = parse_report(_read("eur_app.csv"), _ctx())
    row = parsed.rows[0]

    assert row["event_sales"] == {
        "af_purchase": pytest.approx(250.0),
        "af_subscribe": pytest.approx(90.0),
    }
    assert row["event_unique_users"] == {"af_purchase": 12, "af_subscribe": 3}
    assert row["event_counter"] == {"af_purchase": 14, "af_subscribe": 3}


@pytest.mark.unit
def test_eur_detected_from_headers_with_no_ctx_currency() -> None:
    parsed = parse_report(_read("eur_app.csv"), _ctx())
    assert parsed.currency == "EUR"
    assert all(row["currency"] == "EUR" for row in parsed.rows)
    assert parsed.warnings == []


@pytest.mark.unit
def test_ctx_currency_used_when_it_agrees_with_headers() -> None:
    parsed = parse_report(_read("eur_app.csv"), _ctx(currency="EUR"))
    assert parsed.currency == "EUR"
    assert parsed.warnings == []


@pytest.mark.unit
def test_ctx_currency_used_when_no_sales_headers_at_all() -> None:
    # daily_report.csv carries no event triples at all, so there is nothing
    # to detect from headers — the app-list hint is all there is.
    parsed = parse_report(
        _read("daily_report.csv"), _ctx(currency="JPY", report_type="daily_report")
    )
    assert parsed.currency == "JPY"
    assert all(row["currency"] == "JPY" for row in parsed.rows)
    assert parsed.warnings == []


@pytest.mark.unit
def test_no_currency_known_at_all_stamps_empty_string() -> None:
    parsed = parse_report(_read("daily_report.csv"), _ctx(report_type="daily_report"))
    assert parsed.currency == ""
    assert all(row["currency"] == "" for row in parsed.rows)


@pytest.mark.unit
def test_ctx_currency_disagreeing_with_header_keeps_header_and_warns() -> None:
    # The app-list API said USD, but this report's headers say EUR — AppsFlyer
    # actually returned EUR (it ignores the currency=USD query param on these
    # aggregate reports), so the header wins.
    parsed = parse_report(_read("eur_app.csv"), _ctx(currency="USD"))

    assert parsed.currency == "EUR"
    assert all(row["currency"] == "EUR" for row in parsed.rows)
    assert any("disagrees" in w and "USD" in w and "EUR" in w for w in parsed.warnings)


@pytest.mark.unit
def test_mixed_sales_currencies_in_one_report_warns_and_keeps_all_values() -> None:
    text = (
        "Date,Country,af_purchase (Event counter),af_purchase (Sales in EUR),"
        "af_subscribe (Event counter),af_subscribe (Sales in USD)\n"
        "2026-09-01,DE,5,100.0,2,20.0\n"
    )
    parsed = parse_report(text, _ctx())

    row = parsed.rows[0]
    # Nothing is silently dropped even though the two events disagree on currency.
    assert row["event_sales"] == {
        "af_purchase": pytest.approx(100.0),
        "af_subscribe": pytest.approx(20.0),
    }
    assert any(
        "multiple sales currencies" in w and "EUR" in w and "USD" in w for w in parsed.warnings
    )
    # No single header-detected currency -> falls back to "" (no ctx.currency either).
    assert parsed.currency == ""


# --- duplicate rows (no dedup — MergeTree, product requirement) ------------


@pytest.mark.unit
def test_two_fully_identical_data_rows_both_come_through() -> None:
    """AppsFlyer can return two rows identical on every dimension within one
    pull (same date/country/media_source/... and same metrics) — a product
    requirement is that both count, not just one. The parser must never
    collapse/dedup by content (there is no unique key here — the
    destination is a plain MergeTree, not ReplacingMergeTree)."""
    text = (
        "Date,Media Source (pid),Country,Impressions,Clicks,Installs\n"
        "2026-09-10,googleadwords_int,US,100,10,5\n"
        "2026-09-10,googleadwords_int,US,100,10,5\n"
    )

    parsed = parse_report(text, _ctx())

    assert len(parsed.rows) == 2
    assert parsed.rows[0] == parsed.rows[1]
    # Genuinely two distinct row objects, not one row referenced twice.
    assert parsed.rows[0] is not parsed.rows[1]
