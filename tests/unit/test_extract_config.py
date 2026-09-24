"""Tests for afly.config.extract_config."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from afly.config import ConfigError
from afly.config.extract_config import ExtractConfig, granularity_conflicts_for, warnings_for
from afly.config.project_config import ExtractDefaults, QuotaConfig


def _extract(**overrides) -> ExtractConfig:
    base = {
        "name": "standard",
        "report_type": "geo_by_date_report",
        "table": "appsflyer_geo_by_date",
    }
    base.update(overrides)
    return ExtractConfig.model_validate(base)


@pytest.mark.unit
@pytest.mark.parametrize("report_type", ["partners_report", "geo_report"])
def test_v1_gate_rejects_unsupported_report_types(report_type: str) -> None:
    with pytest.raises(ValidationError, match="not supported in this version"):
        _extract(report_type=report_type)


@pytest.mark.unit
def test_organic_with_media_source_rejected() -> None:
    with pytest.raises(ValidationError, match="organic"):
        _extract(category="organic", media_source="facebook")


@pytest.mark.unit
def test_media_source_and_exclude_media_sources_mutually_exclusive() -> None:
    with pytest.raises(ValidationError, match="mutually exclusive"):
        _extract(media_source="facebook", exclude_media_sources=["yandexdirect_int"])


@pytest.mark.unit
def test_extra_params_forbids_from_and_to() -> None:
    with pytest.raises(ValidationError, match="extra_params"):
        _extract(extra_params={"from": "2026-01-01"})
    with pytest.raises(ValidationError, match="extra_params"):
        _extract(extra_params={"to": "2026-01-01"})


@pytest.mark.unit
def test_with_defaults_fills_unset_fields() -> None:
    extract = _extract()
    defaults = ExtractDefaults(start_date=date(2026, 1, 1), lookback_days=5, chunk_days=3)

    merged = extract.with_defaults(defaults)

    assert merged.start_date == date(2026, 1, 1)
    assert merged.lookback_days == 5
    assert merged.chunk_days == 3
    # original is untouched
    assert extract.start_date is None


@pytest.mark.unit
def test_with_defaults_extract_override_wins() -> None:
    extract = _extract(lookback_days=1)
    defaults = ExtractDefaults(start_date=date(2026, 1, 1), lookback_days=5)

    merged = extract.with_defaults(defaults)

    assert merged.lookback_days == 1


@pytest.mark.unit
def test_with_defaults_missing_start_date_raises_config_error() -> None:
    extract = _extract()
    defaults = ExtractDefaults()  # start_date stays None

    with pytest.raises(ConfigError, match="start_date is required"):
        extract.with_defaults(defaults)


@pytest.mark.unit
def test_resolved_table_bare_name_uses_default_db() -> None:
    extract = _extract(table="my_table")
    assert extract.resolved_table("appsflyer") == ("appsflyer", "my_table")


@pytest.mark.unit
def test_resolved_table_qualified_name() -> None:
    extract = _extract(table="other_db.my_table")
    assert extract.resolved_table("appsflyer") == ("other_db", "my_table")


@pytest.mark.unit
def test_warnings_for_chunk_days_over_budget() -> None:
    extract = _extract(chunk_days=3)
    warnings = warnings_for([extract], QuotaConfig(long_call_min_days=3))
    assert any("chunk_days=3" in w for w in warnings)


@pytest.mark.unit
def test_warnings_for_no_chunk_days_warning_below_budget() -> None:
    extract = _extract(chunk_days=2)
    warnings = warnings_for([extract], QuotaConfig(long_call_min_days=3))
    assert warnings == []


@pytest.mark.unit
def test_warnings_for_covered_media_source_no_warning() -> None:
    standard = _extract(name="standard", exclude_media_sources=["Facebook Ads"])
    facebook = _extract(name="facebook", media_source="facebook")

    warnings = warnings_for([standard, facebook], QuotaConfig())

    assert not any("possible duplicate ownership" in w for w in warnings)


@pytest.mark.unit
def test_warnings_for_uncovered_media_source_warns() -> None:
    standard = _extract(name="standard")  # no exclude_media_sources
    facebook = _extract(name="facebook", media_source="facebook")

    warnings = warnings_for([standard, facebook], QuotaConfig())

    assert any(
        "possible duplicate ownership" in w and "facebook" in w and "standard" in w
        for w in warnings
    )


# -- partition_granularity ---------------------------------------------------


@pytest.mark.unit
def test_partition_granularity_defaults_to_none_before_with_defaults() -> None:
    extract = _extract()
    assert extract.partition_granularity is None


@pytest.mark.unit
def test_with_defaults_fills_partition_granularity_from_project_default() -> None:
    extract = _extract()
    defaults = ExtractDefaults(start_date=date(2026, 1, 1), partition_granularity="day")

    merged = extract.with_defaults(defaults)

    assert merged.partition_granularity == "day"


@pytest.mark.unit
def test_with_defaults_extract_override_wins_for_partition_granularity() -> None:
    extract = _extract(partition_granularity="day")
    defaults = ExtractDefaults(start_date=date(2026, 1, 1))  # project default: "month"

    merged = extract.with_defaults(defaults)

    assert merged.partition_granularity == "day"


@pytest.mark.unit
def test_partition_granularity_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        _extract(partition_granularity="week")


# -- granularity_conflicts_for ------------------------------------------------


@pytest.mark.unit
def test_granularity_conflicts_for_no_conflict_when_same_table_agrees() -> None:
    standard = _extract(name="standard", partition_granularity="month")
    facebook = _extract(name="facebook", partition_granularity="month")

    assert granularity_conflicts_for([standard, facebook]) == []


@pytest.mark.unit
def test_granularity_conflicts_for_different_tables_never_conflict() -> None:
    standard = _extract(
        name="standard", table="appsflyer_geo_by_date", partition_granularity="month"
    )
    other = _extract(name="other", table="a_different_table", partition_granularity="day")

    assert granularity_conflicts_for([standard, other]) == []


@pytest.mark.unit
def test_granularity_conflicts_for_flags_disagreement_on_shared_table() -> None:
    standard = _extract(name="standard", partition_granularity="month")
    facebook = _extract(name="facebook", partition_granularity="day")

    conflicts = granularity_conflicts_for([standard, facebook])

    assert len(conflicts) == 1
    assert "appsflyer_geo_by_date" in conflicts[0]
    assert "standard=month" in conflicts[0]
    assert "facebook=day" in conflicts[0]


@pytest.mark.unit
def test_granularity_conflicts_for_single_extract_never_conflicts() -> None:
    standard = _extract(name="standard", partition_granularity="month")
    assert granularity_conflicts_for([standard]) == []
