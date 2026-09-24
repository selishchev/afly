"""run_pipeline — orchestrates `afly run` end to end.

load context -> select extracts -> resolve apps -> plan -> (dry-run: print
and stop) -> lock + prep destinations -> execute -> release/cleanup -> alert
on failure -> exit code.

Every external dependency lives on :class:`~afly.run._deps.RunDeps`
(re-exported here) so tests can substitute fakes without monkeypatching
module internals.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import click

from afly.appsflyer.errors import AppsFlyerError
from afly.appsflyer.retry import RetryPolicy
from afly.cli._output import echo_done, echo_error, echo_noop
from afly.cli._project import ProjectContext, ProjectError
from afly.config import ConfigError
from afly.config.discovery import load_extracts
from afly.config.selectors import select_extracts
from afly.run._alert import send_run_failure_alerts
from afly.run._apps import build_apps_resolver
from afly.run._deps import RunDeps
from afly.run._setup import TableSetup, owner_string
from afly.run.executor import Executor
from afly.run.options import RunOptions
from afly.run.planner import build_plan, render_plan
from afly.run.scheduler import QuotaScheduler
from afly.run.summary import RunSummary
from afly.utils.datetime_utils import new_run_id


def run_pipeline(options: RunOptions, *, summary: RunSummary, deps: RunDeps | None = None) -> int:
    deps = deps or RunDeps()

    try:
        ctx = deps.load_context(options.profile)
    except (ProjectError, ConfigError) as exc:
        return _fail_early(summary, str(exc))

    summary.project = ctx.project.name
    summary.profile = ctx.profile_name

    try:
        loaded = load_extracts(ctx.root, ctx.project)
    except ConfigError as exc:
        return _fail_early(summary, str(exc))

    extracts_dir = ctx.root / ctx.project.paths.extracts
    selected_all = select_extracts(loaded, options.select, options.exclude, extracts_dir)
    selected = []
    for extract in selected_all:
        if not extract.config.enabled:
            echo_noop(extract.config.name, "disabled")
            continue
        selected.append(extract)

    if not selected:
        return _fail_early(summary, f"no enabled extracts matched selector: {options.select!r}")

    summary.run_id = new_run_id(deps.now())

    client = deps.client_factory(ctx.profile)
    quota = ctx.project.quota
    policy = RetryPolicy(max_retries=quota.max_retries, sleep=deps.sleep)
    apps_resolver = build_apps_resolver(lambda: deps.list_apps(client, policy))

    try:
        # Force-resolve every extract's app list up front (fails fast, and
        # memoizes the account-wide list lookup for build_plan below).
        resolved = {e.config.name: apps_resolver.apps_for(e) for e in selected}
    except AppsFlyerError as exc:
        return _fail_early(
            summary, f"AppsFlyer request failed (status {exc.status}): {exc.body_excerpt}"
        )

    from afly.database.clickhouse import ClickHouseError

    default_db = ctx.profile.clickhouse.database
    manager = None
    try:
        manager = deps.manager_factory(ctx.profile)
        rc = _run_with_manager(
            ctx,
            selected,
            resolved,
            default_db,
            quota,
            options,
            summary,
            deps,
            manager,
            client,
            apps_resolver.currency_for,
        )
    except ClickHouseError as exc:
        # A warehouse failure outside the executor (connect, internal tables,
        # destination DDL, staging) is a run failure, not a traceback: report
        # it, alert once like any other failed run, exit 1. Config mistakes
        # never reach this point (they fail before any connection is made).
        rc = _fail_clickhouse(ctx, summary, deps, exc)
    finally:
        if manager is not None:
            try:
                manager.close()
            except Exception as exc:  # noqa: BLE001
                click.echo(f"  ⚠ failed to close ClickHouse connection: {exc}", err=True)

    return rc


def _run_with_manager(
    ctx: ProjectContext,
    selected: list[Any],
    resolved: dict[str, list[str]],
    default_db: str,
    quota: Any,
    options: RunOptions,
    summary: RunSummary,
    deps: RunDeps,
    manager: Any,
    client: Any,
    currency_for: Callable[[str], str | None],
) -> int:
    # Exception *types* are still imported for real (needed for exact
    # identity in the except clauses below) — everything that actually
    # touches the manager goes through deps' factories instead, so a test
    # can substitute FakeLoadsRepo/FakeLocksRepo/FakeRebuilder without a
    # full fake ClickHouseManager.
    from afly.database.ddl import SchemaMismatchError
    from afly.database.locks import LockHeldError

    internal_db = ctx.profile.clickhouse.internal_db
    deps.ensure_internal_tables(
        manager, internal_db, ctx.project.tables.loads, ctx.project.tables.locks
    )
    loads = deps.loads_repo_factory(manager, internal_db, ctx.project.tables.loads)
    locks = deps.locks_repo_factory(manager, internal_db, ctx.project.tables.locks)

    today = deps.today()
    account_used, app_used = loads.long_calls_today(today)
    summary.quota = {
        "account_long_used": account_used,
        "account_long_budget": quota.account_long_calls_per_day,
        "per_app": dict(app_used),
    }

    plan = build_plan(
        selected,
        apps_for=lambda e: resolved[e.config.name],
        watermark_for=loads.watermark,
        today=today,
        default_db=default_db,
        quota=quota,
        options=options,
    )
    _seed_extract_summaries(summary, plan, default_db)

    if options.dry_run:
        for line in render_plan(plan, quota=quota, account_used=account_used, app_used=app_used):
            click.echo(line)
        summary.finish("dry_run", 0)
        return 0

    if not plan.jobs:
        echo_done("nothing to do")
        summary.finish("success", 0)
        return 0

    owner = owner_string()

    # One partition_granularity per (db, table) — load_extracts already
    # refused to load a project where two extracts sharing a table disagree
    # on it (afly.config.extract_config.granularity_conflicts_for), so any
    # job's own value is representative of every job writing that table.
    table_granularity = {(j.db, j.table): j.extract.config.partition_granularity for j in plan.jobs}

    def _prepare_table(db: str, table: str) -> Any:
        granularity = table_granularity[(db, table)]
        # Guaranteed non-None: every job's extract went through
        # ExtractConfig.with_defaults() in afly.config.discovery.load_extracts.
        assert granularity is not None
        deps.ensure_destination(manager, db, table, granularity)
        staging_db = ctx.profile.clickhouse.staging_database
        if staging_db:
            rebuilder = deps.rebuilder_factory(
                manager, db, table, granularity, staging_db=staging_db
            )
        else:
            rebuilder = deps.rebuilder_factory(manager, db, table, granularity)
        rebuilder.prepare_staging()
        return rebuilder

    with TableSetup(
        locks,
        summary.run_id or "",
        owner,
        ctx.project.lock_timeout_seconds,
        force=options.force,
        prepare_fn=_prepare_table,
    ) as setup:
        try:
            for db, table in sorted(plan.tables()):
                setup.prepare(db, table)
        except LockHeldError as exc:
            db_table = exc.lock_key.removeprefix("table:")
            echo_error(
                f"{db_table} is locked by {exc.owner} (run {exc.run_id}, {exc.age_seconds:.0f}s ago) — "
                f"use `afly unlock --table {db_table}` or --force"
            )
            summary.error = str(exc)
            summary.finish("error", 1)
            return 1
        except SchemaMismatchError as exc:
            echo_error(str(exc))
            summary.error = str(exc)
            summary.finish("error", 1)
            return 1

        scheduler = QuotaScheduler(
            plan.jobs,
            quota,
            account_used=account_used,
            app_used=app_used,
            clock=deps.clock,
            sleep=deps.sleep,
            max_calls=options.max_calls,
            max_minutes=options.max_minutes,
        )
        executor = Executor(
            plan=plan,
            scheduler=scheduler,
            client=client,
            # defer_rate_limits=True: a rate-limited job must not block the
            # rest of the run (see QuotaScheduler.defer) — the executor
            # re-queues it on its own key's delay instead of RetryPolicy
            # sleeping inline. apps_for's own `policy` above stays default
            # (there's no per-key scheduler for the app-list lookup).
            policy_factory=lambda: RetryPolicy(
                max_retries=quota.max_retries, sleep=deps.sleep, defer_rate_limits=True
            ),
            loads=loads,
            rebuilders=setup.rebuilders,
            run_id=summary.run_id or "",
            now=deps.now,
            options=options,
            summary=summary,
            currency_for=currency_for,
        )
        executor.run()

    return _finish_run(ctx, summary, deps)


def _seed_extract_summaries(summary: RunSummary, plan: Any, default_db: str) -> None:
    for ep in plan.extracts:
        db, table = ep.extract.config.resolved_table(default_db)
        summary.extracts.append(
            {
                "name": ep.extract.config.name,
                "table": f"{db}.{table}",
                "apps": len(ep.apps),
                "chunks": len(ep.jobs),
                "succeeded": 0,
                "failed": 0,
                "skipped": 0,
                "rows": 0,
                "api_calls": 0,
                "api_calls_long": 0,
                "window": {"from": ep.window[0], "to": ep.window[1]} if ep.window else None,
            }
        )


def _finish_run(ctx: ProjectContext, summary: RunSummary, deps: RunDeps) -> int:
    failed_jobs = [j for j in summary.jobs if j["status"] == "failed"]
    had_failure = bool(summary.aborted) or bool(failed_jobs)

    if had_failure and ctx.project.error_alerting.enabled:
        send_run_failure_alerts(ctx, summary, deps.alert_sender)

    exit_code = 1 if had_failure else 0
    summary.finish("failed" if had_failure else "success", exit_code)

    totals = summary.totals
    echo_done(
        f"{totals['succeeded']} chunks ok, {totals['failed']} failed, {totals['skipped']} skipped, "
        f"{totals['rows']} rows, {totals['api_calls']} API calls, {totals['days_rebuilt']} partition rebuilds"
    )
    return exit_code


def _fail_clickhouse(
    ctx: ProjectContext, summary: RunSummary, deps: RunDeps, exc: Exception
) -> int:
    message = f"ClickHouse error: {exc}"
    echo_error(message)
    summary.error = message
    summary.aborted = "clickhouse"
    if ctx.project.error_alerting.enabled:
        send_run_failure_alerts(ctx, summary, deps.alert_sender)
    summary.finish("error", 1)
    return 1


def _fail_early(summary: RunSummary, message: str) -> int:
    echo_error(message)
    summary.error = message
    summary.finish("error", 1)
    return 1


__all__ = ["RunDeps", "run_pipeline"]
