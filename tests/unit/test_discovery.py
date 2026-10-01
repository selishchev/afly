"""Tests for afly.config.discovery.load_extracts — beyond what test_init.py and
test_ls_validate.py already exercise through the real scaffold: the
partition_granularity cross-extract check specifically.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from afly.config import ConfigError
from afly.config.discovery import load_extracts
from afly.config.project_config import ProjectConfig


@pytest.mark.unit
def test_load_extracts_resolves_partition_granularity_from_project_default(
    project_dir: Path,
) -> None:
    project = ProjectConfig.from_yaml_file(project_dir / "afly_project.yml")

    loaded = load_extracts(project_dir, project)

    assert {e.config.partition_granularity for e in loaded} == {"month"}


@pytest.mark.unit
def test_load_extracts_raises_on_partition_granularity_conflict(project_dir: Path) -> None:
    project = ProjectConfig.from_yaml_file(project_dir / "afly_project.yml")
    facebook_yml = project_dir / "extracts" / "facebook.yml"
    facebook_yml.write_text(facebook_yml.read_text() + "\npartition_granularity: day\n")

    with pytest.raises(ConfigError, match="partition_granularity"):
        load_extracts(project_dir, project)


@pytest.mark.unit
def test_load_extracts_allows_explicit_agreement_across_extracts(project_dir: Path) -> None:
    """Same non-default value on every extract sharing the table is fine."""
    project = ProjectConfig.from_yaml_file(project_dir / "afly_project.yml")
    for name in ("standard", "facebook", "yandex"):
        p = project_dir / "extracts" / f"{name}.yml"
        p.write_text(p.read_text() + "\npartition_granularity: day\n")

    loaded = load_extracts(project_dir, project)

    assert {e.config.partition_granularity for e in loaded} == {"day"}


@pytest.mark.unit
def test_load_extracts_unions_project_exclude_apps_into_every_extract(project_dir: Path) -> None:
    """`defaults.exclude_apps` (project-wide) is UNIONED into each extract's
    own `exclude_apps:`, not overridden by it — see
    ExtractConfig.with_defaults."""
    project_yml = project_dir / "afly_project.yml"
    project_yml.write_text(
        project_yml.read_text().replace("defaults:\n", "defaults:\n  exclude_apps: ['999']\n", 1)
    )
    facebook_yml = project_dir / "extracts" / "facebook.yml"
    facebook_yml.write_text(facebook_yml.read_text() + "\nexclude_apps: ['111']\n")

    project = ProjectConfig.from_yaml_file(project_yml)
    loaded = load_extracts(project_dir, project)

    by_name = {e.config.name: e.config for e in loaded}
    assert by_name["facebook"].exclude_apps == ["111", "999"]
    # standard/yandex never set their own exclude_apps -> just the project one.
    assert by_name["standard"].exclude_apps == ["999"]
    assert by_name["yandex"].exclude_apps == ["999"]
