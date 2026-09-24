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
