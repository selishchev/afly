"""Shared fixtures for afly's unit test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from afly.cli.commands.init import run_init


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A freshly-scaffolded afly project (via the real `afly init`) under tmp_path.

    Exercising the actual scaffold (rather than hand-writing fixture YAML)
    means every config test also doubles as a regression check on `init` —
    if the scaffold ever stops validating, these tests fail loudly instead
    of the fixture silently drifting from what `init` really produces.
    """
    rc = run_init("demo", str(tmp_path))
    assert rc == 0
    return tmp_path / "demo"


@pytest.fixture
def env_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the five credential env vars the scaffolded profiles.yml references."""
    monkeypatch.setenv("APPSFLYER_TOKEN", "test-token")
    monkeypatch.setenv("CLICKHOUSE_HOST", "localhost")
    monkeypatch.setenv("CLICKHOUSE_USER", "default")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "test-pass")
    monkeypatch.setenv("MATTERMOST_WEBHOOK_URL", "https://example.com/hook")
