"""RunOptions — the frozen, parsed form of `afly run`'s CLI options.

``afly.cli.commands.run.run_command`` turns raw CLI strings (``--from``,
``--apps``, ...) into this dataclass exactly once; everything downstream
(``planner``, ``scheduler``, ``executor``, ``runner``) reads typed fields
instead of re-parsing strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class RunOptions:
    select: str
    exclude: str | None = None
    from_date: date | None = None
    to_date: date | None = None
    full_refresh: bool = False
    dry_run: bool = False
    profile: str | None = None
    json_output: bool = False
    force: bool = False
    apps: list[str] | None = None
    chunk_days: int | None = None
    max_calls: int | None = None
    max_minutes: int | None = None
    allow_empty: bool = False
