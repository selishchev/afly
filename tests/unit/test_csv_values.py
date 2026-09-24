"""Unit tests for afly.csvmap.values — cell-level coercion."""

from __future__ import annotations

from datetime import date

import pytest

from afly.csvmap.values import to_date, to_float, to_str, to_uint


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["", "N/A", "  ", None])
def test_to_str_empty_markers_become_empty_string(raw: str | None) -> None:
    assert to_str(raw) == ""


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["None", "null", "NULL", "-"])
def test_to_str_keeps_literal_placeholders_as_values(raw: str) -> None:
    # AppsFlyer's literal "None" (unattributed campaign/media source) is data
    # that downstream models classify on; dimensions keep it verbatim.
    assert to_str(raw) == raw


@pytest.mark.unit
def test_to_str_strips_whitespace() -> None:
    assert to_str("  US  ") == "US"


@pytest.mark.unit
def test_to_str_keeps_uk_as_is() -> None:
    assert to_str("UK") == "UK"


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["", "N/A", "null", "None", "NULL", "-", None])
def test_to_uint_null_tokens_become_none(raw: str | None) -> None:
    assert to_uint(raw) is None


@pytest.mark.unit
def test_to_uint_parses_plain_integer() -> None:
    assert to_uint("42") == 42


@pytest.mark.unit
def test_to_uint_strips_thousands_separators() -> None:
    assert to_uint("12,000") == 12000


@pytest.mark.unit
def test_to_uint_accepts_trailing_decimal_zero() -> None:
    assert to_uint("12.0") == 12


@pytest.mark.unit
def test_to_uint_negative_is_none() -> None:
    assert to_uint("-5") is None


@pytest.mark.unit
def test_to_uint_unparseable_is_none() -> None:
    assert to_uint("not-a-number") is None


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["", "N/A", "null", "None", "NULL", "-", None])
def test_to_float_null_tokens_become_none(raw: str | None) -> None:
    assert to_float(raw) is None


@pytest.mark.unit
def test_to_float_parses_plain_decimal() -> None:
    assert to_float("3.14") == pytest.approx(3.14)


@pytest.mark.unit
def test_to_float_strips_percent_sign() -> None:
    assert to_float("2.83%") == pytest.approx(2.83)


@pytest.mark.unit
def test_to_float_strips_thousands_and_dollar_sign() -> None:
    assert to_float("$1,234.5") == pytest.approx(1234.5)


@pytest.mark.unit
def test_to_float_negative_is_kept() -> None:
    # Unlike to_uint, negative floats are legitimate (e.g. a negative ROI).
    assert to_float("-12.5") == pytest.approx(-12.5)


@pytest.mark.unit
def test_to_float_unparseable_is_none() -> None:
    assert to_float("garbage") is None


@pytest.mark.unit
def test_to_date_parses_iso_date() -> None:
    assert to_date("2026-09-01") == date(2026, 9, 1)


@pytest.mark.unit
def test_to_date_strips_whitespace() -> None:
    assert to_date("  2026-09-01  ") == date(2026, 9, 1)


@pytest.mark.unit
def test_to_date_unparseable_raises_value_error() -> None:
    with pytest.raises(ValueError, match="2026/09/01"):
        to_date("2026/09/01")
