"""Tests for afly.cli.main — command registration and top-level usage errors."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from afly.cli.main import cli

_EXPECTED_COMMANDS = {
    "init",
    "init-claude",
    "run",
    "apps",
    "ls",
    "validate",
    "debug",
    "unlock",
}


@pytest.mark.unit
def test_help_lists_all_commands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0
    for name in _EXPECTED_COMMANDS:
        assert name in cli.commands
    assert set(cli.commands) == _EXPECTED_COMMANDS


@pytest.mark.unit
def test_run_without_select_exits_2() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["run"])

    assert result.exit_code == 2
    assert "Missing option" in result.output or "required" in result.output.lower()


@pytest.mark.unit
def test_unknown_command_exits_2() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["not-a-real-command"])

    assert result.exit_code == 2


@pytest.mark.unit
def test_version_flag() -> None:
    from afly import __version__

    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])

    assert result.exit_code == 0
    assert __version__ in result.output
