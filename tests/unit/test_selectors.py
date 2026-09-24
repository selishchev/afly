"""Tests for afly.config.selectors (and the duplicate-name guard in discovery)."""

from __future__ import annotations

from pathlib import Path

import pytest

from afly.config import ConfigError
from afly.config.discovery import LoadedExtract, load_extracts
from afly.config.extract_config import ExtractConfig
from afly.config.project_config import ExtractDefaults, ProjectConfig
from afly.config.selectors import select_extracts


def _loaded(name: str, path: str, tags: list[str] | None = None) -> LoadedExtract:
    config = ExtractConfig.model_validate(
        {
            "name": name,
            "report_type": "geo_by_date_report",
            "table": "appsflyer_geo_by_date",
            "tags": tags or [],
        }
    )
    return LoadedExtract(path=Path(path), config=config)


@pytest.fixture
def sample(tmp_path: Path) -> tuple[list[LoadedExtract], Path]:
    root = tmp_path / "extracts"
    items = [
        _loaded("standard", str(root / "standard.yml"), tags=["daily"]),
        _loaded("facebook", str(root / "facebook.yml"), tags=["daily", "paid"]),
        _loaded("yandex", str(root / "geo" / "yandex.yml"), tags=["daily", "late"]),
    ]
    return items, root


@pytest.mark.unit
def test_star_selects_everything(sample) -> None:
    items, root = sample
    assert select_extracts(items, "*", None, root) == items


@pytest.mark.unit
def test_exact_name(sample) -> None:
    items, root = sample
    result = select_extracts(items, "facebook", None, root)
    assert [e.config.name for e in result] == ["facebook"]


@pytest.mark.unit
def test_tag_selector(sample) -> None:
    items, root = sample
    result = select_extracts(items, "tag:paid", None, root)
    assert [e.config.name for e in result] == ["facebook"]


@pytest.mark.unit
def test_glob_on_name(sample) -> None:
    items, root = sample
    result = select_extracts(items, "s*", None, root)
    assert [e.config.name for e in result] == ["standard"]


@pytest.mark.unit
def test_glob_on_path(sample) -> None:
    items, root = sample
    result = select_extracts(items, "geo/*", None, root)
    assert [e.config.name for e in result] == ["yandex"]


@pytest.mark.unit
def test_comma_and_whitespace_separated_list_is_a_union(sample) -> None:
    items, root = sample
    result = select_extracts(items, "standard, facebook   yandex", None, root)
    assert {e.config.name for e in result} == {"standard", "facebook", "yandex"}


@pytest.mark.unit
def test_exclude_subtracts_from_selection(sample) -> None:
    items, root = sample
    result = select_extracts(items, "*", "tag:late", root)
    assert {e.config.name for e in result} == {"standard", "facebook"}


@pytest.mark.unit
def test_exclude_can_use_exact_name(sample) -> None:
    items, root = sample
    result = select_extracts(items, "*", "standard", root)
    assert {e.config.name for e in result} == {"facebook", "yandex"}


@pytest.mark.unit
def test_none_selector_matches_nothing(sample) -> None:
    items, root = sample
    assert select_extracts(items, None, None, root) == []


@pytest.mark.unit
def test_order_follows_discovery_order_not_selector_order(sample) -> None:
    items, root = sample
    result = select_extracts(items, "yandex,standard", None, root)
    assert [e.config.name for e in result] == ["standard", "yandex"]


@pytest.mark.unit
def test_duplicate_extract_names_raise_config_error(tmp_path: Path) -> None:
    extracts_dir = tmp_path / "extracts"
    extracts_dir.mkdir()
    body = "name: dupe\n" "report_type: geo_by_date_report\n" "table: appsflyer_geo_by_date\n"
    (extracts_dir / "a.yml").write_text(body)
    (extracts_dir / "b.yml").write_text(body)

    project = ProjectConfig(
        name="demo",
        default_profile="prod",
        defaults=ExtractDefaults(start_date="2026-01-01"),  # type: ignore[arg-type]
    )

    with pytest.raises(ConfigError, match="duplicate extract name"):
        load_extracts(tmp_path, project)
