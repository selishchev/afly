"""Unit tests for afly.csvmap.headers — header classification and format detection."""

from __future__ import annotations

import pytest

from afly.csvmap.headers import (
    EVENT_KIND_EVENT_COUNTER,
    EVENT_KIND_SALES,
    EVENT_KIND_UNIQUE_USERS,
    KNOWN_HEADERS,
    classify_header,
    detect_format,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("header", "column"),
    [
        ("Date", "date"),
        ("Country", "country"),
        ("Agency/PMD (af_prt)", "agency"),
        ("Media Source (pid)", "media_source"),
        ("Campaign (c)", "campaign"),
        ("Impressions", "impressions"),
        ("Clicks", "clicks"),
        ("CTR", "ctr"),
        ("Installs", "installs"),
        ("Conversion Rate", "conversion_rate"),
        ("Sessions", "sessions"),
        ("Loyal Users", "loyal_users"),
        ("Loyal Users/Installs", "loyal_users_rate"),
        ("Total Revenue", "total_revenue"),
        ("Total Cost", "total_cost"),
        ("ROI", "roi"),
        ("ARPU", "arpu"),
        ("Average eCPI", "average_ecpi"),
        ("Campaign Name", "campaign_name"),
        ("Campaign Id", "campaign_id"),
        ("Adset Name", "adset"),
        ("Adset Id", "adset_id"),
        ("Adgroup Name", "adgroup"),
        ("Adgroup Id", "adgroup_id"),
    ],
)
def test_classify_known_headers(header: str, column: str) -> None:
    result = classify_header(header)
    assert result.kind == "column"
    assert result.name == column
    assert result.case_mismatch is False


@pytest.mark.unit
def test_classify_strips_whitespace() -> None:
    result = classify_header("  Date  ")
    assert result.kind == "column"
    assert result.name == "date"


@pytest.mark.unit
def test_classify_case_insensitive_fallback_flags_mismatch() -> None:
    result = classify_header("date")
    assert result.kind == "column"
    assert result.name == "date"
    assert result.case_mismatch is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("header", "kind"),
    [
        ("af_purchase (Unique users)", EVENT_KIND_UNIQUE_USERS),
        ("af_purchase (Event counter)", EVENT_KIND_EVENT_COUNTER),
    ],
)
def test_classify_event_triple(header: str, kind: str) -> None:
    result = classify_header(header)
    assert result.kind == "event"
    assert result.name == kind
    assert result.event_name == "af_purchase"
    assert result.currency is None
    assert result.case_mismatch is False


@pytest.mark.unit
@pytest.mark.parametrize(
    ("header", "currency"),
    [
        ("af_purchase (Sales in USD)", "USD"),
        ("af_purchase (Sales in EUR)", "EUR"),
        ("af_purchase (Sales in RUB)", "RUB"),
    ],
)
def test_classify_sales_triple_any_currency(header: str, currency: str) -> None:
    # AppsFlyer reports Sales in the app's own currency, not just USD — any
    # 3-letter code is the same triple *kind* (EVENT_KIND_SALES), just
    # carrying a different currency.
    result = classify_header(header)
    assert result.kind == "event"
    assert result.name == EVENT_KIND_SALES
    assert result.event_name == "af_purchase"
    assert result.currency == currency
    assert result.case_mismatch is False


@pytest.mark.unit
def test_classify_event_triple_with_spaces_in_event_name() -> None:
    result = classify_header("My Custom Event (Event counter)")
    assert result.kind == "event"
    assert result.event_name == "My Custom Event"
    assert result.name == "Event counter"


@pytest.mark.unit
def test_classify_event_triple_case_insensitive_fallback() -> None:
    result = classify_header("af_purchase (unique users)")
    assert result.kind == "event"
    assert result.name == "Unique users"
    assert result.event_name == "af_purchase"
    assert result.case_mismatch is True


@pytest.mark.unit
def test_classify_sales_triple_case_insensitive_fallback_uppercases_currency() -> None:
    result = classify_header("af_purchase (sales in eur)")
    assert result.kind == "event"
    assert result.name == EVENT_KIND_SALES
    assert result.event_name == "af_purchase"
    assert result.currency == "EUR"
    assert result.case_mismatch is True


@pytest.mark.unit
def test_classify_unknown_header() -> None:
    result = classify_header("Mystery Metric")
    assert result.kind == "unknown"
    assert result.name is None
    assert result.event_name is None
    assert result.currency is None


@pytest.mark.unit
def test_classified_header_unpacks_as_2_tuple() -> None:
    kind, name = classify_header("Date")
    assert (kind, name) == ("column", "date")


@pytest.mark.unit
def test_known_headers_has_no_duplicate_destination_columns_by_accident() -> None:
    # Every KNOWN_HEADERS value should be a distinct column name — a
    # duplicate would mean two different CSV headers silently collapse
    # into the same destination field.
    values = list(KNOWN_HEADERS.values())
    assert len(values) == len(set(values))


@pytest.mark.unit
def test_detect_format_standard_without_adset_id() -> None:
    headers = ["Date", "Country", "Media Source (pid)", "Campaign (c)", "Impressions"]
    assert detect_format(headers) == "standard"


@pytest.mark.unit
def test_detect_format_facebook_with_adset_id() -> None:
    headers = ["Date", "Country", "Campaign Name", "Adset Name", "Adset Id"]
    assert detect_format(headers) == "facebook"


@pytest.mark.unit
def test_detect_format_strips_whitespace_from_headers() -> None:
    headers = ["Date", " Adset Id "]
    assert detect_format(headers) == "facebook"
