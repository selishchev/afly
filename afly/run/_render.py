"""``render_plan`` — the ``--dry-run`` text renderer, split out of planner.py
purely to keep that module under the house line-length norm.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from afly.config.project_config import QuotaConfig

if TYPE_CHECKING:
    # Deferred: planner.py re-exports render_plan from this module, so a
    # runtime import here would be circular. `from __future__ import
    # annotations` means the `Plan` type hint below is never evaluated at
    # import time, so this is safe as a type-checking-only import.
    from afly.run.planner import Plan


def render_plan(
    plan: Plan, *, quota: QuotaConfig, account_used: int, app_used: dict[str, int]
) -> list[str]:
    """Human-readable ``--dry-run`` lines: per-extract summary + quota/time totals."""
    lines: list[str] = []

    for ep in plan.extracts:
        if ep.note:
            lines.append(f"{ep.extract.config.name}: {ep.note}")
            continue
        long_count = sum(1 for j in ep.jobs if j.is_long)
        assert ep.window is not None
        lines.append(
            f"{ep.extract.config.name}: apps={len(ep.apps)} "
            f"window={ep.window[0]}..{ep.window[1]} chunks={len(ep.jobs)} long_calls={long_count}"
        )

    long_jobs = plan.long_jobs()
    account_limit = quota.account_long_calls_per_day - quota.reserve_long_calls
    account_total = account_used + len(long_jobs)
    account_line = f"account long calls: {account_total} / {account_limit}"
    if account_total > account_limit:
        account_line += " EXCEEDS"
    lines.append(account_line)

    per_app_long: dict[str, int] = {}
    for job in long_jobs:
        per_app_long[job.app_id] = per_app_long.get(job.app_id, 0) + 1
    app_limit = quota.app_long_calls_per_day - quota.reserve_long_calls
    for app_id in sorted(per_app_long):
        total = app_used.get(app_id, 0) + per_app_long[app_id]
        line = f"app {app_id} long calls: {total} / {app_limit}"
        if total > app_limit:
            line += " EXCEEDS"
        lines.append(line)

    per_key: dict[tuple[str, str], int] = {}
    for job in plan.jobs:
        per_key[job.key] = per_key.get(job.key, 0) + 1
    max_per_key = max(per_key.values(), default=0)
    # Two independent lower bounds, take the larger: the busiest key's own
    # per-minute throttle (queue depth * short_call_interval_seconds) can
    # understate the real time once there's more total work than that one
    # key implies — the executor is single-threaded (one HTTP request in
    # flight at a time, see afly.run.executor), so `total_jobs * 2s` is a
    # floor even when every key is individually well under its throttle.
    # Neither term models retries: a rate-limited job now gets deferred
    # (QuotaScheduler.defer) rather than blocking inline, but that still
    # adds real wall time this estimate doesn't account for.
    estimated_minutes = (
        max(max_per_key * quota.short_call_interval_seconds, len(plan.jobs) * 2) / 60
    )

    lines.append(
        f"total calls: {len(plan.jobs)} ({len(long_jobs)} long); "
        f"estimated wall time: {estimated_minutes:.1f} min "
        f"(at ~2s per request, excluding retries)"
    )
    return lines


__all__ = ["render_plan"]
