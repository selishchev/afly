"""Implementation of ``afly ls`` — list extracts and their resolved config.

Purely offline: loads ``afly_project.yml`` and ``extracts/*.yml`` only, never
``profiles.yml`` — you don't need credentials to see what a project would run.
"""

from __future__ import annotations

import json

import click

from afly.cli._output import echo_error, echo_tree
from afly.cli._project import ProjectError, load_project_only
from afly.config import ConfigError
from afly.config.discovery import LoadedExtract, load_extracts
from afly.config.selectors import select_extracts


def _describe(extract: LoadedExtract) -> list[str]:
    config = extract.config
    lines = [
        f"report_type: {config.report_type} (category: {config.category})",
    ]
    if config.media_source:
        lines.append(f"media_source: {config.media_source}")
    lines.append(f"apps: {', '.join(config.apps) if config.apps else 'all account apps'}")
    if config.exclude_apps:
        # Already the UNION of this extract's own `exclude_apps:` and the
        # project's `defaults.exclude_apps:` (ExtractConfig.with_defaults) —
        # this is the one place an operator can see the merged list without
        # reaching for `--json`.
        lines.append(f"exclude_apps: {', '.join(config.exclude_apps)}")
    lines.append(
        f"window: start_date={config.start_date} lookback_days={config.lookback_days} "
        f"chunk_days={config.chunk_days} include_current_day={config.include_current_day}"
    )
    if "." in config.table:
        lines.append(f"table: {config.table}")
    else:
        lines.append(f"table: {config.table} (profile database)")
    lines.append(f"tags: {', '.join(config.tags) if config.tags else '(none)'}")
    lines.append(f"enabled: {config.enabled}")
    return lines


def run_ls(select: str, json_output: bool) -> int:
    """List extracts matching *select*. Returns 1 if the selector matches nothing."""
    try:
        root, project = load_project_only()
        loaded = load_extracts(root, project)
    except (ProjectError, ConfigError) as exc:
        echo_error(str(exc))
        return 1

    matched = select_extracts(loaded, select, None, root / project.paths.extracts)

    if not matched:
        echo_error(f"no extracts matched selector: {select!r}")
        return 1

    if json_output:
        payload = [
            {"path": str(extract.path), **extract.config.model_dump(mode="json")}
            for extract in matched
        ]
        click.echo(json.dumps(payload, indent=2))
        return 0

    for extract in matched:
        echo_tree(extract.config.name, _describe(extract))

    return 0
