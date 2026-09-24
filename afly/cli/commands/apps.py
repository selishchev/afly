"""``afly apps`` — list the AppsFlyer apps visible to the configured token.

Split from ``ls``/``validate`` (offline, config-only commands) because this
one talks to the AppsFlyer management API — a different dependency surface
(network + credentials) that belongs next to the rest of the
``afly.appsflyer`` client code it exercises.
"""

from __future__ import annotations

import json
from typing import Any

from afly.appsflyer.client import AppsFlyerClient
from afly.appsflyer.errors import AppsFlyerError
from afly.appsflyer.mng_api import AppInfo, filter_apps, list_apps
from afly.appsflyer.retry import RetryPolicy
from afly.cli._output import echo_done, echo_error
from afly.cli._project import ProjectContext, ProjectError, load_context
from afly.config import ConfigError


def run_apps(profile: str | None, platform: str | None, json_output: bool) -> int:
    """Fetch and print the AppsFlyer app list for *profile*.

    Returns a process exit code (0 success, 1 failure) — ``cli/main.py``
    turns a non-zero return into ``sys.exit``.
    """
    try:
        ctx = load_context(profile)
    except (ProjectError, ConfigError) as exc:
        echo_error(str(exc))
        return 1

    client = _build_client(ctx)
    policy = RetryPolicy(max_retries=ctx.project.quota.max_retries)

    try:
        apps = list_apps(client, policy)
    except AppsFlyerError as exc:
        echo_error(f"AppsFlyer request failed (status {exc.status}): {exc.body_excerpt}")
        return 1

    apps = filter_apps(apps, [platform] if platform else None)

    if json_output:
        print(json.dumps([_app_to_dict(a) for a in apps], indent=2))
    else:
        _print_table(apps)
    echo_done(f"{len(apps)} app(s)")
    return 0


def _build_client(ctx: ProjectContext) -> AppsFlyerClient:
    af = ctx.profile.appsflyer
    return AppsFlyerClient(
        token=af.token,
        base_url=af.base_url,
        timeout_seconds=af.timeout_seconds,
        user_agent=af.user_agent,
    )


def _app_to_dict(app: AppInfo) -> dict[str, Any]:
    return {
        "id": app.id,
        "name": app.name,
        "platform": app.platform,
        "currency": app.currency,
        "time_zone": app.time_zone,
    }


def _print_table(apps: list[AppInfo]) -> None:
    if not apps:
        print("(no apps)")
        return
    header = ("id", "name", "platform", "currency", "time_zone")
    rows = [header] + [
        (a.id, a.name, a.platform, a.currency or "", a.time_zone or "") for a in apps
    ]
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(header))]
    for row in rows:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))
