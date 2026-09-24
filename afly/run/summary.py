"""RunSummary — the JSON contract for `afly run --json` (schema_version 2).

Every field name/shape here is part of the machine-readable contract other
tools (CI, an alerting dashboard, a future `afly status`) parse — changing a
key is a breaking change, not a refactor. Keep ``schema_version`` in step
with any shape change.

``schema_version`` went 1 -> 2 with configurable partition granularity: each
``days_rebuilt`` entry now reports a ``partition`` (the ``system.parts.
partition_id`` value) and a ``days`` list instead of a single ``day`` — a
rebuild now happens per ClickHouse partition, which under the default
``month`` granularity can span every day a wave touched in that calendar
month, not just one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class RunSummary:
    schema_version: int = 2
    command: str = "run"
    project: str | None = None
    profile: str | None = None
    run_id: str | None = None
    selector: str = ""
    exclude: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    status: str = "success"  # success | failed | dry_run | error
    error: str | None = None
    aborted: str | None = None  # None | "auth" | "clickhouse"
    extracts: list[dict[str, Any]] = field(default_factory=list)
    jobs: list[dict[str, Any]] = field(default_factory=list)
    days_rebuilt: list[dict[str, Any]] = field(default_factory=list)
    quota: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 0

    @property
    def totals(self) -> dict[str, int]:
        succeeded = sum(1 for j in self.jobs if j["status"] == "success")
        failed = sum(1 for j in self.jobs if j["status"] == "failed")
        skipped = sum(1 for j in self.jobs if j["status"] == "skipped")
        return {
            "chunks": len(self.jobs),
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
            "rows": sum(j.get("rows", 0) for j in self.jobs),
            "api_calls": sum(j.get("api_calls", 0) for j in self.jobs),
            "days_rebuilt": len(self.days_rebuilt),
        }

    def finish(self, status: str, exit_code: int, *, finished_at: datetime | None = None) -> None:
        """Stamp the closing fields. Idempotent-ish: safe to call once per run."""
        from afly.utils.datetime_utils import now_utc

        self.finished_at = finished_at if finished_at is not None else now_utc()
        if self.started_at is not None:
            self.duration_seconds = round((self.finished_at - self.started_at).total_seconds(), 3)
        self.status = status
        self.exit_code = exit_code

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "command": self.command,
            "project": self.project,
            "profile": self.profile,
            "run_id": self.run_id,
            "selector": self.selector,
            "exclude": self.exclude,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "status": self.status,
            "error": self.error,
            "aborted": self.aborted,
            "extracts": self.extracts,
            "jobs": self.jobs,
            "days_rebuilt": self.days_rebuilt,
            "quota": self.quota,
            "totals": self.totals,
            "exit_code": self.exit_code,
        }

    def to_json(self) -> str:
        """Serialize per the ``--json`` contract: dates/datetimes as ISO strings."""
        return json.dumps(self.to_dict(), default=str, indent=2)


__all__ = ["RunSummary"]
