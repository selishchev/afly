"""Tests for `afly.cli.commands.unlock.run_unlock` — exit codes and lock clearing.

`load_context` and `ClickHouseManager` (as imported into
`afly.cli.commands.unlock`'s namespace) are monkeypatched so the command runs
against a `FakeManager` instead of a real project/ClickHouse connection.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

import afly.cli.commands.unlock as unlock_mod
from afly.cli._project import ProjectError
from afly.config.profile import ClickHouseProfile
from afly.database.clickhouse import ClickHouseError
from tests.unit.fakes import FakeManager


def _ctx(database: str = "marts") -> SimpleNamespace:
    clickhouse = ClickHouseProfile(host="localhost", database=database)
    return SimpleNamespace(
        profile=SimpleNamespace(clickhouse=clickhouse),
        project=SimpleNamespace(tables=SimpleNamespace(loads="_afly_loads", locks="_afly_locks")),
    )


def _stub_manager_class(fake: FakeManager) -> type:
    class _Stub:
        @classmethod
        def from_profile(cls, profile: Any) -> FakeManager:
            return fake

    return _Stub


def _running_row(lock_key: str, run_id: str = "old-run") -> dict[str, Any]:
    return {
        "lock_key": lock_key,
        "run_id": run_id,
        "status": "running",
        "owner": "someone",
        "started_at": datetime(2026, 9, 1),
        "updated_at": datetime(2026, 9, 1),
        "timeout_seconds": 60,
    }


@pytest.mark.unit
def test_requires_exactly_one_of_table_or_all_neither(capsys: pytest.CaptureFixture[str]) -> None:
    rc = unlock_mod.run_unlock(table=None, all_=False, profile=None)

    assert rc == 1
    assert "exactly one" in capsys.readouterr().err


@pytest.mark.unit
def test_requires_exactly_one_of_table_or_all_both(capsys: pytest.CaptureFixture[str]) -> None:
    rc = unlock_mod.run_unlock(table="af_reports", all_=True, profile=None)

    assert rc == 1
    assert "exactly one" in capsys.readouterr().err


@pytest.mark.unit
def test_project_error_from_load_context(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _raise(profile: str | None) -> Any:
        raise ProjectError("no afly_project.yml found")

    monkeypatch.setattr(unlock_mod, "load_context", _raise)

    rc = unlock_mod.run_unlock(table=None, all_=True, profile=None)

    assert rc == 1
    assert "no afly_project.yml" in capsys.readouterr().err


@pytest.mark.unit
def test_table_clears_held_lock(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeManager()
    fake.query_dicts_default = [_running_row("table:marts.af_reports")]
    monkeypatch.setattr(unlock_mod, "load_context", lambda profile: _ctx())
    monkeypatch.setattr(unlock_mod, "ClickHouseManager", _stub_manager_class(fake))

    rc = unlock_mod.run_unlock(table="af_reports", all_=False, profile=None)

    assert rc == 0
    assert "cleared lock table:marts.af_reports" in capsys.readouterr().out
    assert fake.closed is True


@pytest.mark.unit
def test_table_no_active_lock(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeManager()
    fake.query_dicts_default = []
    monkeypatch.setattr(unlock_mod, "load_context", lambda profile: _ctx())
    monkeypatch.setattr(unlock_mod, "ClickHouseManager", _stub_manager_class(fake))

    rc = unlock_mod.run_unlock(table="af_reports", all_=False, profile=None)

    assert rc == 0
    assert "no active lock" in capsys.readouterr().out


@pytest.mark.unit
def test_all_clears_every_active_lock(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeManager()
    rows = [_running_row("table:marts.a"), _running_row("table:marts.b")]
    # list_active() -> one query_dicts call; clear() per row -> one query_dicts call each.
    fake.query_dicts_queue = [rows, [rows[0]], [rows[1]]]
    monkeypatch.setattr(unlock_mod, "load_context", lambda profile: _ctx())
    monkeypatch.setattr(unlock_mod, "ClickHouseManager", _stub_manager_class(fake))

    rc = unlock_mod.run_unlock(table=None, all_=True, profile=None)

    assert rc == 0
    out = capsys.readouterr().out
    assert "table:marts.a" in out
    assert "table:marts.b" in out
    assert "cleared 2 lock(s)" in out


@pytest.mark.unit
def test_all_no_active_locks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeManager()
    fake.query_dicts_default = []
    monkeypatch.setattr(unlock_mod, "load_context", lambda profile: _ctx())
    monkeypatch.setattr(unlock_mod, "ClickHouseManager", _stub_manager_class(fake))

    rc = unlock_mod.run_unlock(table=None, all_=True, profile=None)

    assert rc == 0
    assert "no active locks" in capsys.readouterr().out


@pytest.mark.unit
def test_clickhouse_error_returns_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeManager()
    fake.raise_on_execute = ClickHouseError("connection refused")
    monkeypatch.setattr(unlock_mod, "load_context", lambda profile: _ctx())
    monkeypatch.setattr(unlock_mod, "ClickHouseManager", _stub_manager_class(fake))

    rc = unlock_mod.run_unlock(table=None, all_=True, profile=None)

    assert rc == 1
    assert "connection refused" in capsys.readouterr().err
    assert fake.closed is True  # finally still ran
