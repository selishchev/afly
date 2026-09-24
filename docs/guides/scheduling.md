# Running on a schedule

afly has no built-in scheduler — drive `afly run` from cron, or from an
orchestrator that can gate on a process exit code or parse `--json` output.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Every chunk succeeded, or was skipped by policy (quota, `--max-calls`/`--max-minutes`, the empty-response guard, a disabled extract). |
| `1` | Any chunk failed, a config/DB error occurred, a destination-table lock is held, or the selector matched no enabled extract. |
| `2` | Usage error (bad CLI arguments). |

Any orchestrator that fails a step on a non-zero exit code already gates
correctly with no further work.

## cron

```cron
# Every 3 hours (8 runs/day)
0 */3 * * * cd /path/to/project && set -a && . .env && set +a && afly run --select "*" --json >> afly.log 2>&1
```

For AppsFlyer accounts whose reports settle within a few hours, 6–8 runs a
day is a reasonable starting cadence — `lookback_days` (default 3) re-covers
any gap between runs automatically via the watermark, so a missed run or two
self-heals on the next one.

## Prefect

A minimal flow that shells out to `afly`, parses the `--json` summary, and
re-raises on failure so the flow run is marked failed (and any
Prefect-level notification/retry policy attached to the flow fires
normally):

```python
import json
import subprocess

from prefect import flow, get_run_logger


@flow(name="afly_standard_run")
def afly_standard_run(project_dir: str = "/path/to/project") -> None:
    logger = get_run_logger()

    result = subprocess.run(
        ["afly", "run", "--select", "*", "--json"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )

    # Human-readable progress went to stderr; stdout carries exactly one
    # JSON document regardless of outcome (see docs/reference/cli.md).
    logger.info(result.stderr)
    summary = json.loads(result.stdout)

    logger.info(
        "afly run %s: %s chunks ok, %s failed, %s skipped, %s rows",
        summary["status"],
        summary["totals"]["succeeded"],
        summary["totals"]["failed"],
        summary["totals"]["skipped"],
        summary["totals"]["rows"],
    )

    if summary["exit_code"] != 0:
        raise RuntimeError(
            f"afly run failed (status={summary['status']}, error={summary.get('error')})"
        )
```

Set a `ConcurrencyLimitConfig(limit=1)` on the deployment if you schedule
more than one run of the same project — afly's own destination-table locks
already prevent two concurrent runs from corrupting a table, but a rejected
run wastes a scheduling slot for no benefit.

## Airflow / other orchestrators

The same shape applies to any tool that can run a shell command and inspect
its exit code or stdout: a `BashOperator`, a Dagster `op`, or a plain CI job
all fail their step naturally when `afly run` exits non-zero — you don't
need `--json` at all if you only care about pass/fail, just check the
process exit code. Use `--json` when you want per-extract detail (rows
loaded, API calls spent, which days were rebuilt) surfaced into your
orchestrator's own logs or outputs.

## Alerting on failure

Scheduling only tells you a run didn't happen or exited non-zero — it
doesn't page anyone by itself. Pair it with afly's own failure alerting
(fires once per run, only on a real failure — never a quota/policy skip) so
someone finds out without watching logs: see [Alerting](alerting.md).
