"""Locate and load an afly project from the current working directory.

Mirrors dbt's "walk up from cwd looking for the project file" convention so
every command can be run from any subdirectory of a project, not just its
root.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from afly.config import ConfigError
from afly.config.profile import ProfileConfig, ProfilesConfig
from afly.config.project_config import ProjectConfig

_PROJECT_FILE = "afly_project.yml"
_PROFILES_FILE = "profiles.yml"
_MAX_UPWARD_LEVELS = 10


class ProjectError(Exception):
    """Raised when an afly project (or its profiles) can't be located or loaded.

    Every message here is written to be actionable on its own — the CLI
    prints ``str(exc)`` directly via ``echo_error`` with no extra wrapping.
    """


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk up from *start* (default: cwd) looking for ``afly_project.yml``.

    Stops after :data:`_MAX_UPWARD_LEVELS` levels (the same bound
    detectkit uses) so a pathological filesystem (or running from ``/``)
    can't turn a missing project into a long search. Returns ``None`` if
    nothing is found.
    """
    current = (start or Path.cwd()).resolve()
    for _ in range(_MAX_UPWARD_LEVELS + 1):
        if (current / _PROJECT_FILE).is_file():
            return current
        if current.parent == current:
            return None
        current = current.parent
    return None


@dataclass
class ProjectContext:
    """Everything a command needs after "load the project and pick a profile"."""

    root: Path
    project: ProjectConfig
    profiles: ProfilesConfig
    profile_name: str
    profile: ProfileConfig


def load_project_only(start: Path | None = None) -> tuple[Path, ProjectConfig]:
    """Load just ``afly_project.yml`` — for commands that never touch credentials.

    Used by ``ls``/``validate``/``init``-adjacent flows that only need the
    project's structure (paths, extract defaults), not a live AppsFlyer/
    ClickHouse connection.
    """
    root = find_project_root(start)
    if root is None:
        cwd = (start or Path.cwd()).resolve()
        raise ProjectError(
            f"No {_PROJECT_FILE} found in {cwd} or its parents — "
            "run `afly init <name>` or cd into a project"
        )
    project = ProjectConfig.from_yaml_file(root / _PROJECT_FILE)
    return root, project


def load_context(
    profile: str | None = None,
    start: Path | None = None,
    *,
    strict_env: bool = True,
) -> ProjectContext:
    """Load the project, ``profiles.yml``, and resolve the active profile.

    Raises :class:`ProjectError` for every failure mode with a message that
    tells the user what to do next: missing project file, missing
    ``profiles.yml``, or an unknown/unset profile name.
    """
    root, project = load_project_only(start)

    profiles_path = root / _PROFILES_FILE
    if not profiles_path.is_file():
        raise ProjectError(
            f"No {_PROFILES_FILE} found at {profiles_path} — "
            "every afly project needs one alongside afly_project.yml"
        )

    try:
        profiles = ProfilesConfig.from_yaml_file(profiles_path, strict_env=strict_env)
    except ConfigError as exc:
        raise ProjectError(str(exc)) from exc

    try:
        profile_name, profile_config = profiles.get_profile(profile)
    except ConfigError as exc:
        raise ProjectError(str(exc)) from exc

    return ProjectContext(
        root=root,
        project=project,
        profiles=profiles,
        profile_name=profile_name,
        profile=profile_config,
    )
