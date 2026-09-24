"""Unit tests for afly.cli.commands.run.run_command — option parsing + --json isolation.

Parameter-validation tests go through the full `cli` click group (so
click.BadParameter really maps to exit code 2 through click's own usage-error
handling); the ``--json`` stdout-isolation tests call ``run_command``
directly with ``afly.cli.commands.run.run_pipeline`` monkeypatched to a stub
— no real project/network/DB needed to exercise the isolation mechanics.
"""

from __future__ import annotations

import json

import click
import pytest
from click.testing import CliRunner

import afly.cli.commands.run as run_cmd_module
from afly.cli.commands.run import run_command
from afly.cli.main import cli
from afly.run.options import RunOptions
from afly.run.summary import RunSummary

_BASE_KWARGS: dict[str, object] = {
    "select": "*",
    "exclude": None,
    "from_date": None,
    "to_date": None,
    "full_refresh": False,
    "dry_run": False,
    "profile": None,
    "json_output": False,
    "force": False,
    "apps": None,
    "chunk_days": None,
    "max_calls": None,
    "max_minutes": None,
    "allow_empty": False,
}


def _kwargs(**overrides: object) -> dict[str, object]:
    merged = dict(_BASE_KWARGS)
    merged.update(overrides)
    return merged


# -- usage errors (through the real click group) -------------------------


@pytest.mark.unit
def test_missing_select_exits_2() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["run"])
    assert result.exit_code == 2


@pytest.mark.unit
def test_bad_from_date_exits_2() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "--select", "*", "--from", "not-a-date"])
    assert result.exit_code == 2
    assert "invalid date" in result.output.lower() or "not-a-date" in result.output


@pytest.mark.unit
def test_bad_to_date_exits_2() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "--select", "*", "--to", "also-not-a-date"])
    assert result.exit_code == 2


# -- --apps parsing -----------------------------------------------------


@pytest.mark.unit
def test_apps_option_splits_on_comma(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, RunOptions] = {}

    def _fake_run_pipeline(options: RunOptions, *, summary: RunSummary) -> int:
        captured["options"] = options
        summary.finish("success", 0)
        return 0

    monkeypatch.setattr(run_cmd_module, "run_pipeline", _fake_run_pipeline)

    run_command(**_kwargs(apps="123, 456,789"))  # type: ignore[arg-type]

    assert captured["options"].apps == ["123", "456", "789"]


# -- --json stdout isolation ------------------------------------------------


@pytest.mark.unit
def test_json_output_is_one_parseable_document_on_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _fake_run_pipeline(options: RunOptions, *, summary: RunSummary) -> int:
        click.echo("a human-readable progress line")
        click.echo("a warning line", err=True)
        summary.project = "demo"
        summary.run_id = "run-123"
        summary.finish("success", 0)
        return 0

    monkeypatch.setattr(run_cmd_module, "run_pipeline", _fake_run_pipeline)

    rc = run_command(**_kwargs(json_output=True))  # type: ignore[arg-type]

    captured = capsys.readouterr()
    assert rc == 0
    doc = json.loads(captured.out)  # raises if stdout has more than one JSON value
    assert doc["project"] == "demo"
    assert doc["run_id"] == "run-123"
    assert doc["exit_code"] == 0
    assert "a human-readable progress line" in captured.err
    assert "a warning line" in captured.err
    assert "a human-readable progress line" not in captured.out


@pytest.mark.unit
def test_json_output_exit_code_mirrors_summary_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _fake_run_pipeline(options: RunOptions, *, summary: RunSummary) -> int:
        summary.finish("failed", 1)
        return 1

    monkeypatch.setattr(run_cmd_module, "run_pipeline", _fake_run_pipeline)

    rc = run_command(**_kwargs(json_output=True))  # type: ignore[arg-type]

    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert rc == 1
    assert doc["exit_code"] == 1
    assert doc["status"] == "failed"


@pytest.mark.unit
def test_json_output_still_emits_one_document_when_pipeline_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _fake_run_pipeline(options: RunOptions, *, summary: RunSummary) -> int:
        click.echo("about to explode")
        raise RuntimeError("unexpected crash")

    monkeypatch.setattr(run_cmd_module, "run_pipeline", _fake_run_pipeline)

    with pytest.raises(RuntimeError, match="unexpected crash"):
        run_command(**_kwargs(json_output=True))  # type: ignore[arg-type]

    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert "RuntimeError" in (doc["error"] or "")
    assert doc["exit_code"] == 1
    assert "about to explode" in captured.err


@pytest.mark.unit
def test_non_json_output_calls_pipeline_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def _fake_run_pipeline(options: RunOptions, *, summary: RunSummary) -> int:
        calls.append(options)
        summary.finish("success", 0)
        return 0

    monkeypatch.setattr(run_cmd_module, "run_pipeline", _fake_run_pipeline)

    rc = run_command(**_kwargs(json_output=False))  # type: ignore[arg-type]

    assert rc == 0
    assert len(calls) == 1
