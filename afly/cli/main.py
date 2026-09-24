"""Main CLI entry point for afly.

Every subcommand callback below lazy-imports its implementation module
inside the function body (not at module load time). This keeps `afly --help`
and `afly --version` instant even though some commands (`run`, `debug`) pull
in heavier dependencies (the ClickHouse driver, the AppsFlyer HTTP client)
transitively — and it means a command belonging to a milestone that hasn't
landed yet can be wired up here in advance: the import only happens, and
only fails, when that specific command is actually invoked.
"""

from __future__ import annotations

import sys

import click

from afly import __version__


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="afly")
def cli() -> None:
    """afly — idempotent AppsFlyer aggregate reports -> ClickHouse.

    Exit codes: 0 = success, 1 = a command ran and failed, 2 = bad usage
    (missing/invalid CLI arguments — click's own convention).

    Run `afly <command> --help` for details on any command.
    """


@cli.command()
@click.argument("project_name")
@click.option(
    "--target-dir",
    "-d",
    default=".",
    help="Directory to create the project in.",
)
def init(project_name: str, target_dir: str) -> None:
    """Scaffold a new afly project.

    Creates afly_project.yml, profiles.yml, and an extracts/ directory with
    three starter extracts (standard, facebook, yandex).

    Example: afly init my_project
    """
    from afly.cli.commands.init import run_init

    rc = run_init(project_name, target_dir)
    if rc:
        sys.exit(rc)


@cli.command(name="init-claude")
@click.option(
    "--target-dir",
    "-d",
    default=".",
    help="Folder holding your afly project(s).",
)
def init_claude(target_dir: str) -> None:
    """Set up Claude Code context for working with afly.

    Example: afly init-claude
    """
    from afly.cli.commands.init_claude import run_init_claude

    rc = run_init_claude(target_dir)
    if rc:
        sys.exit(rc)


@cli.command()
@click.option("--select", "-s", required=True, help="Selector for extracts to run.")
@click.option("--exclude", "-e", help="Selector for extracts to skip.")
@click.option("--from", "from_date", help="Window start date (YYYY-MM-DD).")
@click.option("--to", "to_date", help="Window end date (YYYY-MM-DD).")
@click.option(
    "--full-refresh", is_flag=True, help="Reload every day in the window, ignoring the load ledger."
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the plan (which calls, which windows) without pulling anything.",
)
@click.option("--profile", help="Profile to use (default: from project config).")
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Emit a machine-readable JSON run summary on stdout.",
)
@click.option("--force", is_flag=True, help="Ignore stale extract locks (use with caution).")
@click.option("--apps", help="Comma-separated AppsFlyer app ids to restrict this run to.")
@click.option("--chunk-days", type=int, help="Override the pull chunk size (days) for this run.")
@click.option("--max-calls", type=int, help="Stop after this many AppsFlyer API calls.")
@click.option("--max-minutes", type=int, help="Stop after this many minutes of wall-clock time.")
@click.option(
    "--allow-empty", is_flag=True, help="Don't treat an empty AppsFlyer response as an error."
)
def run(
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
) -> None:
    """Pull AppsFlyer reports and idempotently write them to ClickHouse.

    Example: afly run --select "*" --from 2026-09-01 --to 2026-09-20
    """
    from afly.cli.commands.run import run_command

    rc = run_command(
        select=select,
        exclude=exclude,
        from_date=from_date,
        to_date=to_date,
        full_refresh=full_refresh,
        dry_run=dry_run,
        profile=profile,
        json_output=json_output,
        force=force,
        apps=apps,
        chunk_days=chunk_days,
        max_calls=max_calls,
        max_minutes=max_minutes,
        allow_empty=allow_empty,
    )
    if rc:
        sys.exit(rc)


@cli.command()
@click.option("--profile", help="Profile to use (default: from project config).")
@click.option("--platform", help="Restrict the listing to one platform (ios/android).")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON on stdout.")
def apps(profile: str | None, platform: str | None, json_output: bool) -> None:
    """List the AppsFlyer apps visible to this account.

    Example: afly apps --platform ios
    """
    from afly.cli.commands.apps import run_apps

    rc = run_apps(profile=profile, platform=platform, json_output=json_output)
    if rc:
        sys.exit(rc)


@cli.command()
@click.option(
    "--select", "-s", default="*", show_default=True, help="Selector for extracts to list."
)
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON on stdout.")
def ls(select: str, json_output: bool) -> None:
    """List extracts and their resolved configuration.

    Example: afly ls --select "tag:daily"
    """
    from afly.cli.commands.ls import run_ls

    rc = run_ls(select=select, json_output=json_output)
    if rc:
        sys.exit(rc)


@cli.command()
@click.option(
    "--select", "-s", default="*", show_default=True, help="Selector for extracts to validate."
)
def validate(select: str) -> None:
    """Validate the project config, profiles, and extracts without connecting anywhere.

    Example: afly validate
    """
    from afly.cli.commands.validate import run_validate

    rc = run_validate(select=select)
    if rc:
        sys.exit(rc)


@cli.command()
@click.option("--profile", help="Profile to use (default: from project config).")
@click.option(
    "--deep", is_flag=True, help="Also probe AppsFlyer/ClickHouse connectivity, not just config."
)
@click.option("--pull", "pull", help="Name of one extract to test-pull a small sample for.")
@click.option("--app", "app", help="AppsFlyer app id to scope --pull to.")
@click.option("--from", "from_date", help="Sample window start date (YYYY-MM-DD).")
@click.option("--to", "to_date", help="Sample window end date (YYYY-MM-DD).")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON on stdout.")
def debug(
    profile: str | None,
    deep: bool,
    pull: str | None,
    app: str | None,
    from_date: str | None,
    to_date: str | None,
    json_output: bool,
) -> None:
    """Diagnose a project's config and (optionally) live connectivity.

    Example: afly debug --deep --pull standard --app 123456
    """
    from afly.cli.commands.debug import run_debug

    rc = run_debug(
        profile=profile,
        deep=deep,
        pull=pull,
        app=app,
        from_date=from_date,
        to_date=to_date,
        json_output=json_output,
    )
    if rc:
        sys.exit(rc)


@cli.command()
@click.option("--table", help="Only clear locks held on this destination table.")
@click.option("--all", "all_", is_flag=True, help="Clear every stale lock in the project.")
@click.option("--profile", help="Profile to use (default: from project config).")
def unlock(table: str | None, all_: bool, profile: str | None) -> None:
    """Clear stale extract locks left behind by a run that died mid-flight.

    Example: afly unlock --all
    """
    from afly.cli.commands.unlock import run_unlock

    rc = run_unlock(table=table, all_=all_, profile=profile)
    if rc:
        sys.exit(rc)


if __name__ == "__main__":
    cli()
