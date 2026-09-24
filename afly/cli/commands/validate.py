"""Implementation of ``afly validate`` — config sanity check, no network calls.

Deliberately loads ``profiles.yml`` with ``strict_env=False``: an unset
credential env var is a *warning*, not a failure, because a common use of
this command is exactly "check the project is well-formed before secrets are
provisioned" (e.g. in CI, before deploy). ``afly run``/``afly debug`` still
enforce strict env resolution.
"""

from __future__ import annotations

from afly.cli._output import echo_done, echo_error, echo_warning
from afly.cli._project import ProjectError, load_project_only
from afly.config import ConfigError
from afly.config.discovery import load_extracts
from afly.config.extract_config import warnings_for
from afly.config.profile import ProfilesConfig
from afly.config.selectors import select_extracts

_PROFILES_FILE = "profiles.yml"


def run_validate(select: str) -> int:
    """Validate the project config, profiles, and selected extracts.

    Returns 1 if any layer fails to load/validate, 0 otherwise — warnings
    (unresolved env vars, extract-config warnings) never fail the command.
    """
    had_error = False
    warning_count = 0

    try:
        root, project = load_project_only()
    except (ProjectError, ConfigError) as exc:
        echo_error(str(exc))
        return 1

    profiles_path = root / _PROFILES_FILE
    if not profiles_path.is_file():
        echo_error(f"{profiles_path}: file not found")
        had_error = True
    else:
        try:
            profiles = ProfilesConfig.from_yaml_file(profiles_path, strict_env=False)
        except ConfigError as exc:
            echo_error(str(exc))
            had_error = True
        else:
            for var in profiles.unresolved_env:
                echo_warning(f"{_PROFILES_FILE}: environment variable {var} is not set")
                warning_count += 1

    try:
        loaded = load_extracts(root, project)
    except ConfigError as exc:
        echo_error(str(exc))
        return 1

    try:
        selected = select_extracts(loaded, select, None, root / project.paths.extracts)
    except Exception as exc:  # selector grammar errors, if any surface here
        echo_error(str(exc))
        return 1

    for warning in warnings_for([e.config for e in selected], project.quota):
        echo_warning(warning)
        warning_count += 1

    if had_error:
        return 1

    echo_done(f"{len(selected)} extracts valid, {warning_count} warnings.")
    return 0
