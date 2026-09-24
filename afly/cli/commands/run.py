"""Implementation of ``afly run`` — parse options, run the pipeline, and
(with ``--json``) isolate the machine-readable summary onto the real stdout.

Mirrors detectkit's ``cli/commands/run.py`` ``--json`` pattern:
``contextlib.redirect_stdout(sys.stderr)`` reroutes every ``click.echo`` the
pipeline makes (this module's own plus everything downstream) to stderr for
the duration of the run, leaving the real stdout free for exactly one JSON
document — emitted even if the run dies with an unexpected exception.
"""

from __future__ import annotations

import contextlib
import sys

import click

from afly.run.options import RunOptions
from afly.run.runner import run_pipeline
from afly.run.summary import RunSummary
from afly.utils.datetime_utils import now_utc, parse_date


def run_command(
    select: str,
    exclude: str | None,
    from_date: str | None,
    to_date: str | None,
    full_refresh: bool,
    dry_run: bool,
    profile: str | None,
    json_output: bool,
    force: bool,
    apps: str | None,
    chunk_days: int | None,
    max_calls: int | None,
    max_minutes: int | None,
    allow_empty: bool,
) -> int:
    """Run the pipeline for ``afly run``. Returns the process exit code.

    Raises :class:`click.BadParameter` (a usage error, exit code 2) for an
    unparsable ``--from``/``--to`` — before any run/summary exists, so this
    happens regardless of ``--json``.
    """
    try:
        from_dt = parse_date(from_date) if from_date else None
        to_dt = parse_date(to_date) if to_date else None
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc

    app_list = [a.strip() for a in apps.split(",") if a.strip()] if apps else None

    options = RunOptions(
        select=select,
        exclude=exclude,
        from_date=from_dt,
        to_date=to_dt,
        full_refresh=full_refresh,
        dry_run=dry_run,
        profile=profile,
        json_output=json_output,
        force=force,
        apps=app_list,
        chunk_days=chunk_days,
        max_calls=max_calls,
        max_minutes=max_minutes,
        allow_empty=allow_empty,
    )

    summary = RunSummary(selector=select, exclude=exclude, started_at=now_utc())

    if not json_output:
        return run_pipeline(options, summary=summary)

    rc = 1
    caught: BaseException | None = None
    with contextlib.redirect_stdout(sys.stderr):
        try:
            rc = run_pipeline(options, summary=summary)
        except BaseException as exc:
            caught = exc
            if summary.error is None:
                summary.error = f"{type(exc).__name__}: {exc}"
            if summary.finished_at is None:
                summary.finish("error", 1)
            rc = summary.exit_code

    click.echo(summary.to_json())
    if caught is not None:
        raise caught
    return rc
