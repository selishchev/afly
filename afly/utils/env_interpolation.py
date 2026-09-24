"""Environment variable interpolation for configuration values.

Supports two syntaxes inside string values:

- ``${VAR_NAME}`` — shell-style.
- ``{{ env_var('VAR_NAME') }}`` — dbt-style.

Unresolved placeholders (variable not set) are kept as-is so that callers
get a chance to validate or report missing environment variables instead
of silently falling back to an empty string. This matters for afly
specifically: a silently-empty AppsFlyer token or ClickHouse password
would fail far from the actual cause (an auth error deep in a run), so
``profiles.yml`` loading treats a surviving placeholder as a hard error —
see ``afly.config.profile.ProfilesConfig.from_yaml_file``.
"""

from __future__ import annotations

import os
import re
from typing import Any

_SHELL_PATTERN = re.compile(r"\$\{([^}]+)\}")
_DBT_PATTERN = re.compile(r"\{\{\s*env_var\(['\"]([^'\"]+)['\"]\)\s*\}\}")


def interpolate_env_vars(value: Any) -> Any:
    """Recursively interpolate environment variables in *value*.

    Strings are scanned for both supported placeholder syntaxes; mappings
    and sequences are walked depth-first. Other types pass through
    unchanged.
    """
    if isinstance(value, str):
        return _interpolate_string(value)
    if isinstance(value, dict):
        return {k: interpolate_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate_env_vars(item) for item in value]
    if isinstance(value, tuple):
        return tuple(interpolate_env_vars(item) for item in value)
    return value


def _interpolate_string(value: str) -> str:
    value = _SHELL_PATTERN.sub(
        lambda m: os.environ.get(m.group(1), m.group(0)),
        value,
    )
    value = _DBT_PATTERN.sub(
        lambda m: os.environ.get(m.group(1), m.group(0)),
        value,
    )
    return value


def find_unresolved(value: Any) -> list[str]:
    """Return the env-var names still present as unresolved placeholders in *value*.

    Walks the same (nested) shapes as :func:`interpolate_env_vars` — after
    that function has already run over *value* — and collects every
    ``${VAR}`` / ``{{ env_var('VAR') }}`` placeholder that survived because
    the variable was not set. Used by ``ProfilesConfig.from_yaml_file`` to
    turn "a credential silently stayed a literal string" into a named,
    actionable error instead of a downstream connection failure.
    """
    found: list[str] = []
    _collect_unresolved(value, found)
    return found


def _collect_unresolved(value: Any, found: list[str]) -> None:
    if isinstance(value, str):
        found.extend(_SHELL_PATTERN.findall(value))
        found.extend(_DBT_PATTERN.findall(value))
    elif isinstance(value, dict):
        for v in value.values():
            _collect_unresolved(v, found)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_unresolved(item, found)
