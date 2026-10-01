"""Tests for afly.config.project_config.ProjectConfig."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from afly.config import ConfigError
from afly.config.project_config import ExtractDefaults, ProjectConfig, QuotaConfig


@pytest.mark.unit
def test_minimal_config_applies_defaults() -> None:
    config = ProjectConfig(name="demo", default_profile="prod")

    assert config.version == "1.0"
    assert config.paths.extracts == "extracts"
    assert config.tables.loads == "_afly_loads"
    assert config.tables.locks == "_afly_locks"
    assert config.defaults.lookback_days == 3
    assert config.defaults.chunk_days == 2
    assert config.defaults.on_empty == "skip"
    assert config.defaults.partition_granularity == "month"
    assert config.quota.account_long_calls_per_day == 120
    assert config.lock_timeout_seconds == 7200
    assert config.error_alerting.enabled is False


@pytest.mark.unit
def test_partition_granularity_accepts_day() -> None:
    config = ProjectConfig(
        name="demo", default_profile="prod", defaults={"partition_granularity": "day"}
    )
    assert config.defaults.partition_granularity == "day"


@pytest.mark.unit
def test_partition_granularity_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        ProjectConfig(
            name="demo", default_profile="prod", defaults={"partition_granularity": "week"}
        )


@pytest.mark.unit
def test_rejects_unknown_top_level_key() -> None:
    with pytest.raises(ValidationError):
        ProjectConfig(name="demo", default_profile="prod", totally_unknown_key=1)  # type: ignore[call-arg]


@pytest.mark.unit
def test_rejects_unknown_nested_key() -> None:
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(
            {
                "name": "demo",
                "default_profile": "prod",
                "defaults": {"chunck_days": 2},  # typo, not a real field
            }
        )


@pytest.mark.unit
def test_lock_timeout_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        ProjectConfig(name="demo", default_profile="prod", lock_timeout_seconds=30)

    with pytest.raises(ValidationError):
        ProjectConfig(name="demo", default_profile="prod", lock_timeout_seconds=999_999)


@pytest.mark.unit
def test_chunk_days_range_enforced() -> None:
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(
            {"name": "demo", "default_profile": "prod", "defaults": {"chunk_days": 0}}
        )
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(
            {"name": "demo", "default_profile": "prod", "defaults": {"chunk_days": 91}}
        )


@pytest.mark.unit
def test_from_yaml_file_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "afly_project.yml"
    path.write_text(yaml.safe_dump({"name": "demo", "default_profile": "prod", "version": "1.0"}))
    config = ProjectConfig.from_yaml_file(path)
    assert config.name == "demo"
    assert config.default_profile == "prod"


@pytest.mark.unit
def test_from_yaml_file_missing_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        ProjectConfig.from_yaml_file(tmp_path / "does_not_exist.yml")


@pytest.mark.unit
def test_from_yaml_file_empty_raises_config_error(tmp_path: Path) -> None:
    path = tmp_path / "afly_project.yml"
    path.write_text("")
    with pytest.raises(ConfigError, match="empty"):
        ProjectConfig.from_yaml_file(path)


@pytest.mark.unit
def test_quota_config_retry_defaults() -> None:
    quota = QuotaConfig()
    assert quota.transient_base_wait_seconds == 30.0
    assert quota.transient_max_wait_seconds == 600.0
    assert quota.retry_jitter == 0.25
    assert quota.max_waves_in_flight == 8


@pytest.mark.unit
def test_quota_config_retry_policy_kwargs_maps_the_three_fields() -> None:
    quota = QuotaConfig(
        transient_base_wait_seconds=10.0, transient_max_wait_seconds=100.0, retry_jitter=0.1
    )
    assert quota.retry_policy_kwargs() == {
        "transient_base_wait": 10.0,
        "transient_cap": 100.0,
        "retry_jitter": 0.1,
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "field, value",
    [
        ("transient_base_wait_seconds", -1.0),
        ("transient_max_wait_seconds", -1.0),
        ("retry_jitter", -0.01),
        ("retry_jitter", 1.01),
    ],
)
def test_quota_config_rejects_out_of_range_retry_fields(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        QuotaConfig(**{field: value})


@pytest.mark.unit
def test_extract_defaults_exclude_apps_defaults_empty_and_rejects_typo() -> None:
    assert ExtractDefaults().exclude_apps == []
    with pytest.raises(ValidationError):
        ExtractDefaults.model_validate({"exclue_apps": ["x"]})  # typo'd key


@pytest.mark.unit
def test_from_yaml_file_invalid_data_wraps_validation_error(tmp_path: Path) -> None:
    path = tmp_path / "afly_project.yml"
    path.write_text(yaml.safe_dump({"name": "demo"}))  # missing required default_profile
    with pytest.raises(ConfigError, match=re.escape(str(path))):
        ProjectConfig.from_yaml_file(path)
