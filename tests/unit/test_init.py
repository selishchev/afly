"""Tests for afly.cli.commands.init.run_init."""

from __future__ import annotations

from pathlib import Path

import pytest

from afly.cli.commands.init import run_init
from afly.config.discovery import load_extracts
from afly.config.extract_config import warnings_for
from afly.config.profile import ProfilesConfig
from afly.config.project_config import ProjectConfig

_EXPECTED_FILES = [
    "afly_project.yml",
    "profiles.yml",
    "extracts/standard.yml",
    "extracts/facebook.yml",
    "extracts/yandex.yml",
    ".env.example",
    ".gitignore",
    "README.md",
]


@pytest.mark.unit
def test_creates_all_expected_files(tmp_path: Path) -> None:
    rc = run_init("demo", str(tmp_path))
    assert rc == 0

    project_root = tmp_path / "demo"
    for relative in _EXPECTED_FILES:
        assert (project_root / relative).is_file(), f"missing {relative}"


@pytest.mark.unit
def test_refuses_to_overwrite_existing_directory(tmp_path: Path) -> None:
    (tmp_path / "demo").mkdir()

    rc = run_init("demo", str(tmp_path))

    assert rc == 1
    # nothing was written into the pre-existing directory
    assert list((tmp_path / "demo").iterdir()) == []


@pytest.mark.unit
def test_scaffold_loads_through_real_models_with_zero_errors(project_dir: Path) -> None:
    project = ProjectConfig.from_yaml_file(project_dir / "afly_project.yml")
    assert project.name == "demo"

    loaded = load_extracts(project_dir, project)
    assert {e.config.name for e in loaded} == {"standard", "facebook", "yandex"}

    # profiles.yml validates even with no credentials in the environment
    profiles = ProfilesConfig.from_yaml_file(project_dir / "profiles.yml", strict_env=False)
    assert "prod" in profiles.profiles
    assert profiles.default_profile == "prod"


@pytest.mark.unit
def test_scaffold_has_no_ownership_warning(project_dir: Path) -> None:
    project = ProjectConfig.from_yaml_file(project_dir / "afly_project.yml")
    loaded = load_extracts(project_dir, project)

    warnings = warnings_for([e.config for e in loaded], project.quota)

    assert not any("possible duplicate ownership" in w for w in warnings)


@pytest.mark.unit
def test_start_date_is_first_of_previous_month(project_dir: Path) -> None:
    from afly.cli.commands.init import _first_day_of_previous_month
    from afly.utils.datetime_utils import today_utc

    project = ProjectConfig.from_yaml_file(project_dir / "afly_project.yml")

    assert project.defaults.start_date == _first_day_of_previous_month(today_utc())
    assert project.defaults.start_date.day == 1
