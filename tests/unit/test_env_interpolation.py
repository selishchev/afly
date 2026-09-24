"""Tests for afly.utils.env_interpolation."""

from __future__ import annotations

import pytest

from afly.utils.env_interpolation import find_unresolved, interpolate_env_vars


@pytest.mark.unit
def test_shell_syntax_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOO", "bar")
    assert interpolate_env_vars("${FOO}") == "bar"


@pytest.mark.unit
def test_dbt_syntax_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOO", "bar")
    assert interpolate_env_vars("{{ env_var('FOO') }}") == "bar"
    assert interpolate_env_vars('{{ env_var("FOO") }}') == "bar"


@pytest.mark.unit
def test_unresolved_kept_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOES_NOT_EXIST", raising=False)
    assert interpolate_env_vars("${DOES_NOT_EXIST}") == "${DOES_NOT_EXIST}"
    assert (
        interpolate_env_vars("{{ env_var('DOES_NOT_EXIST') }}") == "{{ env_var('DOES_NOT_EXIST') }}"
    )


@pytest.mark.unit
def test_nested_structures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOST", "clickhouse.local")
    data = {
        "profiles": {
            "prod": {
                "clickhouse": {"host": "${HOST}", "tags": ["a", "${HOST}"]},
            }
        },
        "pair": ("${HOST}", 1),
    }
    result = interpolate_env_vars(data)
    assert result["profiles"]["prod"]["clickhouse"]["host"] == "clickhouse.local"
    assert result["profiles"]["prod"]["clickhouse"]["tags"] == ["a", "clickhouse.local"]
    assert result["pair"] == ("clickhouse.local", 1)


@pytest.mark.unit
def test_non_string_types_pass_through() -> None:
    assert interpolate_env_vars(42) == 42
    assert interpolate_env_vars(None) is None
    assert interpolate_env_vars(True) is True


@pytest.mark.unit
def test_find_unresolved_flat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_A", raising=False)
    monkeypatch.delenv("MISSING_B", raising=False)
    value = interpolate_env_vars("${MISSING_A} and {{ env_var('MISSING_B') }}")
    assert sorted(find_unresolved(value)) == ["MISSING_A", "MISSING_B"]


@pytest.mark.unit
def test_find_unresolved_nested(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_C", raising=False)
    monkeypatch.setenv("PRESENT", "ok")
    data = {
        "a": ["${PRESENT}", "${MISSING_C}"],
        "b": {"c": "{{ env_var('MISSING_C') }}"},
    }
    resolved = interpolate_env_vars(data)
    assert find_unresolved(resolved) == ["MISSING_C", "MISSING_C"]


@pytest.mark.unit
def test_find_unresolved_empty_when_all_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOO", "bar")
    resolved = interpolate_env_vars({"x": "${FOO}"})
    assert find_unresolved(resolved) == []
