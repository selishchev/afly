# afly — CLI (`afly`)

Run all commands from a project directory (the one containing
`afly_project.yml`, found by walking up from cwd — any subdirectory works).
`afly --help` and `afly <command> --help` always work.

## Commands

| Command | Purpose |
|---|---|
| `afly init <name>` | Scaffold a new project directory |
| `afly init-claude` | (Re)generate this Claude context (`CLAUDE.md` + `.claude/rules/afly/` + skills) |
| `afly run --select <sel>` | Pull AppsFlyer reports and idempotently write them to ClickHouse |
| `afly ls --select <sel>` | List extracts and their resolved (post-default-merge) config, offline |
| `afly validate` | Config sanity check — no network calls |
| `afly debug` | Diagnose config + live AppsFlyer/ClickHouse connectivity, optionally sample-pull one extract |
| `afly apps` | List the AppsFlyer apps visible to the configured token |
| `afly unlock` | Clear a stale/held destination-table lock left by a crashed run |
| `afly --version` | Show the installed afly version |

## Selectors (`--select` / `-s`, `--exclude` / `-e`)

Used by `run`, `ls`, and `validate`. Items are separated by commas and/or
whitespace; each one of:

- **`*`** — every extract.
- **`tag:<t>`** — extracts whose `tags:` list contains `<t>`.
- **a glob** (`fnmatch` syntax) tested against both the extract's `name` and
  its file path relative to `extracts/` — an exact name (`standard`) is just
  a glob with no wildcard.

Multiple items in `--select` are a union; `--exclude` is subtracted from that
union afterward. Result order always follows discovery order, not selector
order. `afly run` additionally drops any selected extract with `enabled:
false` (logged as a no-op, not an error) before planning.

## Exit codes (`afly run`)

| Code | Meaning |
|---|---|
| `0` | Every chunk succeeded, or was skipped by policy (a quota/`--max-calls`/`--max-minutes` stop, the empty-response guard, or a disabled extract) — nothing that afly considers a failure happened. |
| `1` | Any chunk failed, a config/DB error occurred, a destination-table lock is held by another run, or the selector matched no *enabled* extract. |
| `2` | Usage error — bad CLI arguments (e.g. an unparsable `--from`/`--to` date). Click's own convention. |

Every other command follows Click's default (0 on success, non-zero on any
unhandled exception); `ls`/`validate`/`apps`/`debug`/`unlock` each return 1 on
their own failure modes (see `afly <command> --help`).

## `afly run`

```bash
afly run --select <sel> [--exclude <sel>] [--from YYYY-MM-DD] [--to YYYY-MM-DD] \
         [--full-refresh] [--dry-run] [--profile NAME] [--json] [--force] \
         [--apps id1,id2] [--chunk-days N] [--max-calls N] [--max-minutes N] \
         [--allow-empty]
```

| Option | Meaning |
|---|---|
| `--select` / `-s` (required) | Selector for extracts to run. |
| `--exclude` / `-e` | Selector for extracts to skip. |
| `--from` | Window start date, overrides the computed watermark-based start (still floored at the extract's `start_date`). |
| `--to` | Window end date, overrides "today" (or "yesterday" if `include_current_day: false`). |
| `--full-refresh` | Reload every day in the window from `start_date`, ignoring the watermark — use after a query/mapping change. |
| `--dry-run` | Print the plan (per-extract window, chunk/long-call counts, estimated wall time, quota totals) and stop — pulls nothing, writes nothing. |
| `--profile` | Profile to use (default: `profiles.yml`'s `default_profile`). |
| `--json` | Emit exactly one JSON summary document on stdout (schema below); all human-readable progress moves to stderr for the duration of the run. |
| `--force` | Ignore a held destination-table lock and take it anyway. Only after confirming the previous run actually died — see `afly-debug-run`. |
| `--apps` | Comma-separated AppsFlyer app ids to restrict this run to (intersected with each extract's own `apps:`/`platforms:`). |
| `--chunk-days` | Override every selected extract's `chunk_days` for this run only. |
| `--max-calls` | Stop scheduling once this many AppsFlyer API calls have been made this run; remaining jobs are recorded `skipped`. The watermark makes the next run continue where this one stopped. |
| `--max-minutes` | Same stop behavior, on wall-clock elapsed time. |
| `--allow-empty` | Disable the empty-response guard for this run — an empty AppsFlyer response is applied (clears the day) even where the extract's `on_empty` is `skip`. |

### Window logic (per pair)

`end` = today (or yesterday, if `include_current_day: false`), clamped by
`--to`. `start` = `--from` if given; else `start_date` if this is a
`--full-refresh` or the pair has never loaded successfully; else the earlier
of `today − lookback_days` and `watermark − lookback_days + 1` (auto-closes a
gap after downtime, and re-opens the last `lookback_days` before the
watermark for late-settling attribution), floored at `start_date`. A window
with `start > end` means "nothing to do" for that pair, not an error.

### `--json` output shape (`schema_version: 2`)

```json
{
  "schema_version": 2,
  "command": "run",
  "project": "...", "profile": "...", "run_id": "...",
  "selector": "...", "exclude": null,
  "started_at": "...", "finished_at": "...", "duration_seconds": 0.0,
  "status": "success | failed | dry_run | error",
  "error": null,
  "aborted": null,
  "extracts": [{"name": "...", "table": "db.table", "apps": 0, "chunks": 0,
                "succeeded": 0, "failed": 0, "skipped": 0, "rows": 0,
                "api_calls": 0, "api_calls_long": 0,
                "window": {"from": "...", "to": "..."}}],
  "jobs": [{"extract": "...", "app_id": "...", "from": "...", "to": "...",
            "status": "success | failed | skipped", "rows": 0, "api_calls": 0,
            "http_status": 200, "error": null, "skip_reason": null}],
  "days_rebuilt": [{"table": "db.table", "partition": "202609",
                     "days": ["2026-09-01", "2026-09-02"],
                     "action": "replace | drop | noop",
                     "kept_rows": 0, "fresh_rows": 0}],
  "quota": {"account_long_used": 0, "account_long_budget": 120, "per_app": {}},
  "totals": {"chunks": 0, "succeeded": 0, "failed": 0, "skipped": 0,
             "rows": 0, "api_calls": 0, "days_rebuilt": 0},
  "exit_code": 0
}
```

`days_rebuilt` is keyed by **partition**, not day (`schema_version` 1 -> 2):
under the default `partition_granularity: month`, one entry can cover every
day of a calendar month a wave touched, not just one — `"days"` lists the
specific days *of this rebuild* (a month partition can be rebuilt by several
waves across one run; each entry's `"days"` is only that wave's own days).
`totals.days_rebuilt` therefore now counts partition-rebuilds, not days.

`status: "error"` means the run never produced a plan at all (config load
failure, no enabled extracts matched); `"aborted"` is set to `"auth"` (a 401,
or a 403 that never resolved into a real quota response after retries) or
`"clickhouse"` (a partition-rebuild failure) when the whole run stopped early
— everything not yet processed is recorded `skipped` with that reason.

### Backfill recipes

```bash
# Everything, from scratch (first run on a fresh ClickHouse)
afly run --select "*" --dry-run          # check the plan and quota totals first
afly run --select "*"

# Reload one extract after a config change (e.g. added a new event)
afly run --select standard --full-refresh --from 2026-01-01

# Backfill a wide window without blowing the daily long-call budget —
# stay in the per-minute tier by keeping chunk_days small (see quotas.md)
afly run --select "*" --from 2024-01-01 --chunk-days 2 --max-calls 500

# One app only, useful while debugging a single account's data
afly run --select standard --apps 123456789
```

## `afly ls`

```bash
afly ls [--select "*"] [--json]
```

Offline — loads `afly_project.yml` and `extracts/*.yml` only, never
`profiles.yml`. Prints each matched extract's resolved `report_type`,
`category`, `media_source`, `apps`, window config, destination `table`,
`tags`, `enabled`. `--json` emits each extract's full resolved config
(`ExtractConfig.model_dump()`) plus its file path.

## `afly validate`

```bash
afly validate [--select "*"]
```

Loads the project, `extracts/*.yml`, and `profiles.yml` with
**`strict_env=False`** — an unset credential env var is a *warning*, not a
failure, since this command is meant to pass in CI before secrets are
provisioned. Also surfaces `extract_config.warnings_for()`: a `chunk_days`
at/above `quota.long_call_min_days` (heads-up: this extract draws the daily
budget), and a likely double-counted media source between an unfiltered
extract and a filtered sibling writing the same table (the
`exclude_media_sources` ownership check — see `extracts.md`). Returns 1 if
any layer fails to load; warnings never fail it.

## `afly debug`

```bash
afly debug [--profile NAME] [--deep] [--pull EXTRACT --app ID [--from D] [--to D]] [--json]
```

Runs a fixed check list (project config loads, `profiles.yml` loads with env
vars resolved, the AppsFlyer management API is reachable) plus the ClickHouse
checks from `afly.database.checks`: connect, `SELECT 1`, database/
internal-database exist-or-creatable, a best-effort `SHOW GRANTS` scan for
ALTER/INSERT/CREATE TABLE. `--deep` additionally proves the exact
partition-swap mechanics `idempotency.md` describes on **this** server, using
two disposable `__afly_debug_*` tables (always dropped): create, insert,
`REPLACE PARTITION`, `DROP PARTITION`, and — informationally — what happens
replacing from a source partition that doesn't exist (the empty-staging
finding).

`--pull EXTRACT --app ID` additionally does one real Pull API call (window
defaults to yesterday..yesterday, capped at 2 days total) and reports the
detected CSV format, headers, unknown headers, row count, and up to 3 sample
rows — without touching ClickHouse at all. Use this to check a new extract's
shape (e.g. whether `category: facebook` was actually needed — see
`formats.md`) before wiring it into a real run.

## `afly apps`

```bash
afly apps [--profile NAME] [--platform ios|android] [--json]
```

Lists every app the configured AppsFlyer token can see (paginated
management-API call). Cross-check `apps:`/`exclude_apps:`/`platforms:` in an
extract against this list — an app id that doesn't appear here will
(silently) resolve to zero pairs for that extract.

## `afly unlock`

```bash
afly unlock (--table db.table | --all) [--profile NAME]
```

Every `afly run` takes one lock per destination table it writes
(`_afly_locks`, keyed `table:<db>.<table>`) and releases it on exit — even on
failure. A lock only survives a **crash** (killed process, machine reboot,
OOM). `afly unlock` clears it immediately, bypassing the normal staleness
check `run`'s own lock-acquire uses (which auto-recovers a `running` row older
than `lock_timeout_seconds`, default 7200s, on its own). Exactly one of
`--table`/`--all` is required.

## Common workflows

```bash
# First run of a fresh project
afly validate
afly debug
afly run --select "*" --dry-run
afly run --select "*"

# Recover from a run that died mid-flight
afly unlock --all
afly run --select "*"          # resumes from the watermark, nothing is duplicated

# Diagnose why an extract's numbers look wrong
afly debug --pull <extract> --app <id>
afly ls --select <extract>
```

## Scheduling

afly has no built-in scheduler — drive `afly run` from cron, Prefect, Airflow,
or any orchestrator that can gate on a process exit code. See
`docs/guides/scheduling.md` for a cron line and a Prefect flow example that
parses `--json`'s `exit_code`.
