"""``afly debug`` — diagnose a profile: config loads, AppsFlyer reachability,
and (optionally) a one-off report pull, all without writing to ClickHouse.

Each base check returns a :class:`CheckResult`; ``run_debug`` collects them
into one name→result map. The ClickHouse-side checks live in
``afly.database.checks`` (connect / database / grants, and under ``--deep`` a
real REPLACE/DROP PARTITION round-trip on scratch tables) and are merged into
the same result map, so the human and ``--json`` renderings stay uniform.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import click

from afly.appsflyer.client import AppsFlyerClient
from afly.appsflyer.errors import AppsFlyerError
from afly.appsflyer.mng_api import AppInfo, list_apps
from afly.appsflyer.pull_api import PullRequestSpec, fetch_report
from afly.appsflyer.retry import RetryPolicy
from afly.cli._output import echo_done, echo_error, echo_warning
from afly.cli._project import ProjectContext, ProjectError, load_context
from afly.config import ConfigError
from afly.config.discovery import load_extracts
from afly.csvmap.parser import ReportContext, parse_report
from afly.utils.datetime_utils import new_run_id, now_utc, parse_date, today_utc

# Inclusive span of a --pull debug window: yesterday..yesterday (1 day) is
# the default, and the window may grow to at most this many calendar days.
_MAX_PULL_WINDOW_DAYS = 2


@dataclass
class CheckResult:
    ok: bool
    detail: str


def _build_client(ctx: ProjectContext) -> AppsFlyerClient:
    af = ctx.profile.appsflyer
    return AppsFlyerClient(
        token=af.token,
        base_url=af.base_url,
        timeout_seconds=af.timeout_seconds,
        user_agent=af.user_agent,
    )


def _check_project_config(ctx: ProjectContext) -> CheckResult:
    return CheckResult(True, f"project config loaded from {ctx.root}")


def _check_profiles(ctx: ProjectContext) -> CheckResult:
    # load_context() already ran env interpolation and raises ProjectError
    # on any placeholder still unresolved — reaching this check at all means
    # profiles.yml parsed and every credential resolved.
    return CheckResult(True, f"profile '{ctx.profile_name}' loaded, credentials resolved")


def _cached_apps_lister(ctx: ProjectContext) -> Callable[[], list[AppInfo]]:
    """Memoize one account-wide ``list_apps`` call for this ``run_debug`` invocation.

    Both the "AppsFlyer mng API reachable" check and (when ``--pull`` is
    also given) the per-app currency lookup need the app list — sharing one
    cached call means an interactive ``afly debug --pull ... --app ...``
    spends it once, not twice.
    """
    cache: list[AppInfo] | None = None

    def _list() -> list[AppInfo]:
        nonlocal cache
        if cache is None:
            policy = RetryPolicy(max_retries=ctx.project.quota.max_retries)
            cache = list_apps(_build_client(ctx), policy)
        return cache

    return _list


def _check_mng_api(list_apps_fn: Callable[[], list[AppInfo]]) -> CheckResult:
    try:
        apps = list_apps_fn()
    except AppsFlyerError as exc:
        return CheckResult(False, f"unreachable (status {exc.status}): {exc.body_excerpt}")
    return CheckResult(True, f"reachable, {len(apps)} app(s) visible")


def run_debug(
    profile: str | None,
    deep: bool,
    pull: str | None,
    app: str | None,
    from_date: str | None,
    to_date: str | None,
    json_output: bool,
) -> int:
    """Run the debug checks for *profile*; returns a process exit code."""
    try:
        ctx = load_context(profile)
    except (ProjectError, ConfigError) as exc:
        echo_error(str(exc))
        return 1

    list_apps_fn = _cached_apps_lister(ctx)
    results = {
        "project config loads": _check_project_config(ctx),
        "profiles.yml loads and env vars resolved": _check_profiles(ctx),
        "AppsFlyer mng API reachable": _check_mng_api(list_apps_fn),
    }

    results.update(_clickhouse_results(ctx, deep=deep))

    pull_summary: dict[str, Any] | None = None
    pull_ok = True
    if pull is not None:
        if not app:
            echo_error("--pull requires --app")
            return 1
        try:
            window_start, window_end = _resolve_window(from_date, to_date)
        except ValueError as exc:
            echo_error(str(exc))
            return 1
        pull_ok, pull_summary = _run_pull_check(
            ctx, pull, app, window_start, window_end, list_apps_fn
        )

    all_ok = all(r.ok for r in results.values()) and pull_ok

    if json_output:
        payload: dict[str, Any] = {
            name: {"ok": r.ok, "detail": r.detail} for name, r in results.items()
        }
        if pull_summary is not None:
            payload["pull"] = pull_summary
        print(json.dumps(payload, indent=2, default=str))
    else:
        for name, result in results.items():
            if result.ok:
                click.echo(click.style(f"  ✓ {name}: {result.detail}", fg="green"))
            else:
                echo_error(f"{name}: {result.detail}")
        if pull_summary is not None:
            _print_pull_summary(pull_summary)
        passed = sum(1 for r in results.values() if r.ok)
        echo_done(f"{passed}/{len(results)} checks passed" + ("" if all_ok else ", see ✗ above"))

    return 0 if all_ok else 1


def _clickhouse_results(ctx: ProjectContext, *, deep: bool) -> dict[str, CheckResult]:
    """Run the ClickHouse-side checks and adapt them to this module's result map.

    Imported lazily so ``afly debug --help`` never pays for ``clickhouse_driver``.
    A driver-level exception is itself the finding (unreachable host, bad
    credentials) — it is reported as a failed check rather than a traceback.
    """
    from afly.database.checks import run_clickhouse_checks

    try:
        outcomes = run_clickhouse_checks(
            ctx.profile.clickhouse,
            deep=deep,
            granularity=ctx.project.defaults.partition_granularity,
        )
    except Exception as exc:  # noqa: BLE001 — any driver error is the diagnosis here
        return {"ClickHouse reachable": CheckResult(False, f"{type(exc).__name__}: {exc}")}
    return {outcome.name: CheckResult(outcome.ok, outcome.detail) for outcome in outcomes}


def _resolve_window(from_date: str | None, to_date: str | None) -> tuple[date, date]:
    """Resolve --from/--to (default: yesterday..yesterday), capped at 2 days."""
    yesterday = today_utc() - timedelta(days=1)
    start = parse_date(from_date) if from_date else yesterday
    end = parse_date(to_date) if to_date else yesterday
    if (end - start).days >= _MAX_PULL_WINDOW_DAYS:
        raise ValueError(
            f"--pull debug window may not exceed {_MAX_PULL_WINDOW_DAYS} days "
            "(default: yesterday..yesterday) — narrow --from/--to"
        )
    return start, end


def _run_pull_check(
    ctx: ProjectContext,
    extract_name: str,
    app_id: str,
    from_date: date,
    to_date: date,
    list_apps_fn: Callable[[], list[AppInfo]],
) -> tuple[bool, dict[str, Any]]:
    try:
        extracts = load_extracts(ctx.root, ctx.project)
    except ConfigError as exc:
        return False, {"error": str(exc)}

    loaded = next((e for e in extracts if e.config.name == extract_name), None)
    if loaded is None:
        return False, {"error": f"extract {extract_name!r} not found"}

    spec = PullRequestSpec.from_extract(loaded.config)
    policy = RetryPolicy(max_retries=ctx.project.quota.max_retries)

    try:
        raw = fetch_report(_build_client(ctx), spec, app_id, from_date, to_date, policy)
    except AppsFlyerError as exc:
        return False, {"error": f"fetch failed (status {exc.status}): {exc.body_excerpt}"}

    # Best-effort: the mng-API app list may itself be unreachable (that's
    # its own, already-reported check) — a --pull check shouldn't fail just
    # because the currency hint couldn't be fetched, since parse_report
    # falls back to detecting it from the CSV's own headers anyway.
    try:
        currency = next((a.currency for a in list_apps_fn() if a.id == app_id), None)
    except AppsFlyerError:
        currency = None

    report_ctx = ReportContext(
        app_id=app_id,
        report_type=spec.report_type,
        category=spec.category,
        is_retargeting=spec.reattr,
        extract=extract_name,
        run_id=new_run_id(),
        loaded_at=now_utc(),
        currency=currency,
    )
    parsed = parse_report(
        raw.text,
        report_ctx,
        date_range=(from_date, to_date),
        exclude_media_sources=loaded.config.exclude_media_sources,
        keep_unknown_columns=bool(loaded.config.keep_unknown_columns),
    )

    return True, {
        "status": raw.status,
        "elapsed_ms": raw.elapsed_ms,
        "currency": parsed.currency,
        "api_calls": raw.api_calls,
        "format": parsed.format,
        "headers": parsed.headers,
        "unknown_headers": parsed.unknown_headers,
        "warnings": parsed.warnings,
        "row_count": len(parsed.rows),
        "dropped_out_of_range": parsed.dropped_out_of_range,
        "dropped_excluded": parsed.dropped_excluded,
        "sample_rows": parsed.rows[:3],
    }


def _print_pull_summary(summary: dict[str, Any]) -> None:
    if "error" in summary:
        echo_error(f"pull: {summary['error']}")
        return
    print(
        f"  pull: status={summary['status']} elapsed_ms={summary['elapsed_ms']} "
        f"api_calls={summary['api_calls']} format={summary['format']} "
        f"currency={summary['currency'] or '(none detected)'}"
    )
    print(f"  headers: {', '.join(summary['headers'])}")
    if summary["unknown_headers"]:
        echo_warning(f"unknown headers: {', '.join(summary['unknown_headers'])}")
    # ParsedReport.warnings also repeats the unknown-headers warning above
    # (in its own wording) — skip it here so it isn't printed twice.
    for warning in summary["warnings"]:
        if not warning.startswith("unknown CSV headers:"):
            echo_warning(warning)
    print(
        f"  rows={summary['row_count']} dropped_out_of_range={summary['dropped_out_of_range']} "
        f"dropped_excluded={summary['dropped_excluded']}"
    )
    for row in summary["sample_rows"]:
        print(f"    {row}")
