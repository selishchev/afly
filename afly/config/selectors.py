"""dbt-style ``--select``/``--exclude`` matching over loaded extracts.

Selector grammar (shared by every command that takes ``--select``): items
separated by commas and/or whitespace, each one of

- ``*`` — everything;
- ``tag:<t>`` — extracts whose ``tags:`` list contains ``<t>``;
- a glob (``fnmatch`` syntax) tested against both the extract's ``name`` and
  its file path relative to the extracts root — an exact name is just a
  glob with no wildcard characters, so it needs no special case.

Multiple items in ``--select`` are a union; ``--exclude`` is subtracted from
that union afterwards. Result order always follows discovery order (never
selector-item order), so output is stable regardless of how ``--select`` was
phrased.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from afly.config.discovery import LoadedExtract

_SPLIT_RE = re.compile(r"[\s,]+")


def _parse_selector(selector: str) -> list[str]:
    return [item for item in _SPLIT_RE.split(selector.strip()) if item]


def _matches_item(item: str, extract: LoadedExtract, root: Path) -> bool:
    if item == "*":
        return True
    if item.startswith("tag:"):
        return item[len("tag:") :] in extract.config.tags
    try:
        rel = extract.path.relative_to(root).as_posix()
    except ValueError:
        rel = extract.path.as_posix()
    return fnmatch.fnmatch(extract.config.name, item) or fnmatch.fnmatch(rel, item)


def select_extracts(
    loaded: list[LoadedExtract],
    select: str | None,
    exclude: str | None,
    root: Path,
) -> list[LoadedExtract]:
    """Filter *loaded* by the ``--select``/``--exclude`` selector grammar above.

    ``select=None`` (or an all-whitespace string) matches nothing — callers
    that want "everything" pass an explicit ``"*"`` (which is what every CLI
    command's ``--select`` defaults to).
    """
    include_items = _parse_selector(select) if select else []
    exclude_items = _parse_selector(exclude) if exclude else []

    selected = [e for e in loaded if any(_matches_item(i, e, root) for i in include_items)]
    if exclude_items:
        selected = [
            e for e in selected if not any(_matches_item(i, e, root) for i in exclude_items)
        ]
    return selected
