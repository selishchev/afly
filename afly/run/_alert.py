"""Dispatch the once-per-run failure alert, if the project wants one.

Only called when the run actually failed (an abort, or at least one failed
job) — never for quota/policy skips alone and never for a config-load error
(those return before a ``ProjectContext``/``RunSummary`` pair even exists to
alert about).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from afly.cli._output import echo_warning
from afly.utils.env_interpolation import find_unresolved

if TYPE_CHECKING:
    from afly.cli._project import ProjectContext
    from afly.run.summary import RunSummary

_MAX_LISTED_FAILURES = 10


def send_run_failure_alerts(ctx: ProjectContext, summary: RunSummary, alert_sender: Any) -> None:
    cfg = ctx.project.error_alerting
    lines = _build_lines(ctx, summary)

    for name in cfg.channels:
        channel = ctx.profiles.alert_channels.get(name)
        if channel is None:
            echo_warning(f"unknown alert channel '{name}' — skipping")
            continue
        if not channel.webhook_url or find_unresolved(channel.webhook_url):
            echo_warning(f"alert channel '{name}' has no webhook_url set — skipping")
            continue
        ok = alert_sender(
            channel,
            project=ctx.project.name,
            profile=ctx.profile_name,
            run_id=summary.run_id or "",
            title=f"afly run failed: {ctx.project.name}/{ctx.profile_name}",
            lines=lines,
            mentions=cfg.mentions,
        )
        if not ok:
            echo_warning(f"failed to send alert to channel '{name}'")


def _build_lines(ctx: ProjectContext, summary: RunSummary) -> list[str]:
    lines = [
        f"Run {summary.run_id} for project {ctx.project.name} (profile {ctx.profile_name}) failed."
    ]
    if summary.aborted:
        lines.append(f"Aborted: {summary.aborted}")
    if summary.error:
        lines.append(summary.error)

    failed = [j for j in summary.jobs if j["status"] == "failed"]
    for job in failed[:_MAX_LISTED_FAILURES]:
        lines.append(f"{job['extract']} {job['app_id']} {job['from']}..{job['to']}: {job['error']}")
    if len(failed) > _MAX_LISTED_FAILURES:
        lines.append(f"... and {len(failed) - _MAX_LISTED_FAILURES} more")

    return lines


__all__ = ["send_run_failure_alerts"]
