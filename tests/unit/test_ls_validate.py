"""Tests for `afly ls` and `afly validate` through the real CLI (CliRunner)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from afly.cli.main import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _invoke_in(runner: CliRunner, cwd: Path, args: list[str]):
    old = Path.cwd()
    os.chdir(cwd)
    try:
        return runner.invoke(cli, args)
    finally:
        os.chdir(old)


@pytest.mark.unit
def test_ls_lists_three_extracts(runner: CliRunner, project_dir: Path) -> None:
    result = _invoke_in(runner, project_dir, ["ls"])

    assert result.exit_code == 0
    assert "standard" in result.output
    assert "facebook" in result.output
    assert "yandex" in result.output


@pytest.mark.unit
def test_ls_json_parses_and_has_three_entries(runner: CliRunner, project_dir: Path) -> None:
    result = _invoke_in(runner, project_dir, ["ls", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) == 3
    assert {entry["name"] for entry in payload} == {"standard", "facebook", "yandex"}
    assert all("path" in entry for entry in payload)


@pytest.mark.unit
def test_ls_no_match_exits_1(runner: CliRunner, project_dir: Path) -> None:
    result = _invoke_in(runner, project_dir, ["ls", "--select", "nomatch"])
    assert result.exit_code == 1


@pytest.mark.unit
def test_ls_outside_project_exits_1_with_actionable_message(
    runner: CliRunner, tmp_path: Path
) -> None:
    empty_dir = tmp_path / "not_a_project"
    empty_dir.mkdir()

    result = _invoke_in(runner, empty_dir, ["ls"])

    assert result.exit_code == 1
    assert "afly init" in result.output


@pytest.mark.unit
def test_validate_succeeds_with_env_var_warnings_when_unset(
    runner: CliRunner, project_dir: Path
) -> None:
    result = _invoke_in(runner, project_dir, ["validate"])

    assert result.exit_code == 0
    assert "environment variable" in result.output
    assert "Done." in result.output


@pytest.mark.unit
def test_validate_no_warnings_when_env_set(
    runner: CliRunner, project_dir: Path, env_creds: None
) -> None:
    result = _invoke_in(runner, project_dir, ["validate"])

    assert result.exit_code == 0
    assert "0 warnings" in result.output


@pytest.mark.unit
def test_validate_outside_project_exits_1(runner: CliRunner, tmp_path: Path) -> None:
    empty_dir = tmp_path / "not_a_project"
    empty_dir.mkdir()

    result = _invoke_in(runner, empty_dir, ["validate"])

    assert result.exit_code == 1


@pytest.mark.unit
def test_validate_fails_on_partition_granularity_conflict_across_extracts(
    runner: CliRunner, project_dir: Path
) -> None:
    """standard/facebook/yandex all write appsflyer_geo_by_date — making one of
    them override partition_granularity away from the others must be a hard
    validate failure, not a warning (a destination table has one PARTITION BY)."""
    facebook_yml = project_dir / "extracts" / "facebook.yml"
    facebook_yml.write_text(facebook_yml.read_text() + "\npartition_granularity: day\n")

    result = _invoke_in(runner, project_dir, ["validate"])

    assert result.exit_code == 1
    assert "partition_granularity" in result.output
    assert "appsflyer_geo_by_date" in result.output
