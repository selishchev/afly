"""Tests for afly.config.profile."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from afly.config import ConfigError
from afly.config.profile import ClickHouseProfile, ProfilesConfig

_BASE = {
    "default_profile": "prod",
    "profiles": {
        "prod": {
            "appsflyer": {"token": "{{ env_var('APPSFLYER_TOKEN') }}"},
            "clickhouse": {
                "host": "{{ env_var('CLICKHOUSE_HOST') }}",
                "user": "{{ env_var('CLICKHOUSE_USER') }}",
                "password": "{{ env_var('CLICKHOUSE_PASSWORD') }}",
                "database": "appsflyer",
            },
        }
    },
    "alert_channels": {
        "ops": {"type": "mattermost", "webhook_url": "{{ env_var('MATTERMOST_WEBHOOK_URL') }}"}
    },
}


def _write_profiles(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "profiles.yml"
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.mark.unit
def test_strict_env_raises_and_names_missing_vars(tmp_path: Path) -> None:
    path = _write_profiles(tmp_path, _BASE)
    with pytest.raises(ConfigError) as exc_info:
        ProfilesConfig.from_yaml_file(path, strict_env=True)

    message = str(exc_info.value)
    assert "APPSFLYER_TOKEN" in message
    assert "CLICKHOUSE_HOST" in message
    assert "CLICKHOUSE_USER" in message
    assert "CLICKHOUSE_PASSWORD" in message
    assert "MATTERMOST_WEBHOOK_URL" not in message  # an unset webhook only disables that channel


@pytest.mark.unit
def test_strict_env_passes_when_vars_set(tmp_path: Path, env_creds: None) -> None:
    path = _write_profiles(tmp_path, _BASE)
    config = ProfilesConfig.from_yaml_file(path, strict_env=True)

    assert config.profiles["prod"].appsflyer.token == "test-token"
    assert config.profiles["prod"].clickhouse.host == "localhost"
    assert config.unresolved_env == []


@pytest.mark.unit
def test_non_strict_collects_unresolved_instead_of_raising(tmp_path: Path) -> None:
    path = _write_profiles(tmp_path, _BASE)
    config = ProfilesConfig.from_yaml_file(path, strict_env=False)

    assert set(config.unresolved_env) == {
        "APPSFLYER_TOKEN",
        "CLICKHOUSE_HOST",
        "CLICKHOUSE_USER",
        "CLICKHOUSE_PASSWORD",
    }
    # the literal placeholder text survives into the model when unresolved
    assert config.profiles["prod"].appsflyer.token == "{{ env_var('APPSFLYER_TOKEN') }}"


@pytest.mark.unit
def test_unresolved_env_excluded_from_model_dump(tmp_path: Path, env_creds: None) -> None:
    path = _write_profiles(tmp_path, _BASE)
    config = ProfilesConfig.from_yaml_file(path, strict_env=False)
    assert "unresolved_env" not in config.model_dump()


@pytest.mark.unit
def test_default_profile_must_exist() -> None:
    with pytest.raises(ValidationError):
        ProfilesConfig.model_validate(
            {
                "default_profile": "missing",
                "profiles": {
                    "prod": {
                        "appsflyer": {"token": "x"},
                        "clickhouse": {"host": "h", "database": "d"},
                    }
                },
            }
        )


@pytest.mark.unit
def test_get_profile_by_name() -> None:
    config = ProfilesConfig.model_validate(
        {
            "profiles": {
                "prod": {
                    "appsflyer": {"token": "x"},
                    "clickhouse": {"host": "h", "database": "d"},
                },
                "dev": {
                    "appsflyer": {"token": "y"},
                    "clickhouse": {"host": "h2", "database": "d2"},
                },
            }
        }
    )
    name, profile = config.get_profile("dev")
    assert name == "dev"
    assert profile.appsflyer.token == "y"


@pytest.mark.unit
def test_get_profile_falls_back_to_default() -> None:
    config = ProfilesConfig.model_validate(
        {
            "default_profile": "prod",
            "profiles": {
                "prod": {
                    "appsflyer": {"token": "x"},
                    "clickhouse": {"host": "h", "database": "d"},
                }
            },
        }
    )
    name, profile = config.get_profile(None)
    assert name == "prod"


@pytest.mark.unit
def test_get_profile_unknown_name_raises_config_error() -> None:
    config = ProfilesConfig.model_validate(
        {
            "profiles": {
                "prod": {
                    "appsflyer": {"token": "x"},
                    "clickhouse": {"host": "h", "database": "d"},
                }
            }
        }
    )
    with pytest.raises(ConfigError, match="unknown profile"):
        config.get_profile("nope")


@pytest.mark.unit
def test_get_profile_no_name_no_default_raises_config_error() -> None:
    config = ProfilesConfig.model_validate(
        {
            "profiles": {
                "prod": {
                    "appsflyer": {"token": "x"},
                    "clickhouse": {"host": "h", "database": "d"},
                }
            }
        }
    )
    with pytest.raises(ConfigError, match="no --profile"):
        config.get_profile(None)


@pytest.mark.unit
def test_internal_db_falls_back_to_database() -> None:
    config = ProfilesConfig.model_validate(
        {
            "profiles": {
                "prod": {
                    "appsflyer": {"token": "x"},
                    "clickhouse": {"host": "h", "database": "appsflyer"},
                }
            }
        }
    )
    assert config.profiles["prod"].clickhouse.internal_db == "appsflyer"

    config2 = ProfilesConfig.model_validate(
        {
            "profiles": {
                "prod": {
                    "appsflyer": {"token": "x"},
                    "clickhouse": {
                        "host": "h",
                        "database": "appsflyer",
                        "internal_database": "afly_internal",
                    },
                }
            }
        }
    )
    assert config2.profiles["prod"].clickhouse.internal_db == "afly_internal"


@pytest.mark.unit
def test_base_url_trailing_slash_stripped() -> None:
    config = ProfilesConfig.model_validate(
        {
            "profiles": {
                "prod": {
                    "appsflyer": {"token": "x", "base_url": "https://hq1.appsflyer.com/"},
                    "clickhouse": {"host": "h", "database": "d"},
                }
            }
        }
    )
    assert config.profiles["prod"].appsflyer.base_url == "https://hq1.appsflyer.com"


# ── protocol / effective_port ────────────────────────────────────────────


@pytest.mark.unit
def test_protocol_defaults_to_native() -> None:
    profile = ClickHouseProfile(host="h", database="d")
    assert profile.protocol == "native"


@pytest.mark.unit
def test_existing_profile_with_explicit_port_9000_keeps_working() -> None:
    """Backward compatibility: a profile written before `protocol` existed still parses."""
    profile = ClickHouseProfile(host="h", database="d", port=9000)
    assert profile.protocol == "native"
    assert profile.port == 9000
    assert profile.effective_port == 9000


@pytest.mark.unit
@pytest.mark.parametrize(
    ("protocol", "secure", "expected"),
    [
        ("native", False, 9000),
        ("native", True, 9440),
        ("http", False, 8123),
        ("http", True, 8443),
    ],
)
def test_effective_port_defaults_by_protocol_and_secure(
    protocol: str, secure: bool, expected: int
) -> None:
    profile = ClickHouseProfile(host="h", database="d", protocol=protocol, secure=secure)  # type: ignore[arg-type]
    assert profile.port is None
    assert profile.effective_port == expected


@pytest.mark.unit
def test_effective_port_honours_an_explicit_port_over_the_protocol_default() -> None:
    profile = ClickHouseProfile(host="h", database="d", protocol="http", port=8424)
    assert profile.effective_port == 8424


@pytest.mark.unit
def test_protocol_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        ClickHouseProfile(host="h", database="d", protocol="ftp")  # type: ignore[arg-type]


@pytest.mark.unit
def test_unset_alert_webhook_does_not_block_loading(tmp_path, monkeypatch) -> None:
    """A declared channel with its env var unset only disables that channel."""
    for var in ("APPSFLYER_TOKEN", "CLICKHOUSE_HOST", "CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD"):
        monkeypatch.setenv(var, "x")
    monkeypatch.setenv("MATTERMOST_WEBHOOK_URL", "")
    path = tmp_path / "profiles.yml"
    path.write_text(
        "profiles:\n  prod:\n    appsflyer: {token: \"{{ env_var('APPSFLYER_TOKEN') }}\"}\n"
        "    clickhouse: {host: h, database: d}\n"
        "alert_channels:\n  ops: {type: mattermost, webhook_url: \"{{ env_var('MATTERMOST_WEBHOOK_URL') }}\"}\n"
    )
    config = ProfilesConfig.from_yaml_file(path, strict_env=True)
    assert config.alert_channels["ops"].webhook_url == ""
