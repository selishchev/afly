"""Finding and loading every ``extracts/*.yml`` file in a project.

Split from ``selectors.py`` on purpose: discovery+loading answers "what
extracts does this project define" (a filesystem + parsing concern),
selection answers "which of those does this command run" (a CLI-input
concern) — keeping them separate means `afly ls` can load everything once
and then run several selector questions against the same in-memory list.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from afly.config import ConfigError
from afly.config.extract_config import ExtractConfig, granularity_conflicts_for
from afly.config.project_config import ProjectConfig


def discover_extract_files(extracts_dir: Path) -> list[Path]:
    """All ``*.yml``/``*.yaml`` files under *extracts_dir*, recursively.

    Skips any path with a hidden component (a leading dot on any directory
    or file name) — the same convention detectkit uses to keep an editor's
    scratch dir or a future archive folder from being discovered as a live
    extract. Returns a sorted list for deterministic ordering.
    """
    files = [
        p
        for pattern in ("**/*.yml", "**/*.yaml")
        for p in extracts_dir.glob(pattern)
        if p.is_file()
        and not any(part.startswith(".") for part in p.relative_to(extracts_dir).parts)
    ]
    return sorted(set(files))


@dataclass
class LoadedExtract:
    """One discovered extract: its file path, and its config with project
    defaults already merged in (:meth:`ExtractConfig.with_defaults`)."""

    path: Path
    config: ExtractConfig


def load_extracts(root: Path, project: ProjectConfig) -> list[LoadedExtract]:
    """Discover, parse, and default-merge every extract under the project.

    Raises :class:`ConfigError` for: a missing extracts directory, a
    duplicate ``name:`` across two files (extract names are the selector
    and idempotency key — a collision would make ``--select`` ambiguous and
    could cross-contaminate the load ledger), any individual file's
    parse/validation error (wrapped with its path), or two extracts sharing
    one ``table:`` that resolve to different ``partition_granularity``
    values (see :func:`~afly.config.extract_config.granularity_conflicts_for`)
    — a destination table has exactly one ``PARTITION BY``.
    """
    extracts_dir = root / project.paths.extracts
    if not extracts_dir.is_dir():
        raise ConfigError(
            f"extracts directory not found: {extracts_dir} "
            f"(configured via paths.extracts in afly_project.yml)"
        )

    loaded: list[LoadedExtract] = []
    seen: dict[str, Path] = {}

    for path in discover_extract_files(extracts_dir):
        config = ExtractConfig.from_yaml_file(path).with_defaults(project.defaults)

        if config.name in seen:
            raise ConfigError(
                f"duplicate extract name '{config.name}':\n"
                f"  - {seen[config.name]}\n"
                f"  - {path}\n"
                "Extract names must be unique across the project."
            )
        seen[config.name] = path
        loaded.append(LoadedExtract(path=path, config=config))

    conflicts = granularity_conflicts_for([le.config for le in loaded])
    if conflicts:
        raise ConfigError("\n".join(conflicts))

    return loaded
