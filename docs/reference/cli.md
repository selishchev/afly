# CLI reference

Run all commands from a project directory (containing `afly_project.yml`) or
any subdirectory of one — afly walks up looking for it. `afly --help` and
`afly <command> --help` always work.

## `afly init`

```bash
afly init <project_name> [--target-dir/-d DIR]
```

| Option | Default | Meaning |
|---|---|---|
| `project_name` (argument) | — | Directory name to create. |
| `--target-dir` / `-d` | `.` | Parent directory to create the project in. |

Fails (exit 1) if the target directory already exists.

## `afly init-claude`

```bash
afly init-claude [--target-dir/-d DIR]
```

| Option | Default | Meaning |
|---|---|---|
| `--target-dir` / `-d` | `.` | Folder holding your afly project(s). |

Always exits 0. See [Claude Code guide](../guides/claude-code.md).

## `afly run`

```bash
afly run --select SEL [--exclude SEL] [--from DATE] [--to DATE] \
         [--full-refresh] [--dry-run] [--profile NAME] [--json] [--force] \
         [--apps IDS] [--chunk-days N] [--max-calls N] [--max-minutes N] \
         [--allow-empty]
```

| Option | Default | Meaning |
|---|---|---|
| `--select` / `-s` | *(required)* | Selector for extracts to run — see [Selectors](#selectors). |
| `--exclude` / `-e` | none | Selector for extracts to skip. |
| `--from` | computed from watermark | Window start date (`YYYY-MM-DD`). |
| `--to` | today (or yesterday) | Window end date (`YYYY-MM-DD`). |
| `--full-refresh` | off | Reload every day in the window, ignoring the watermark. |
| `--dry-run` | off | Print the plan and stop — no AppsFlyer calls, no ClickHouse writes. |
| `--profile` | `profiles.yml`'s `default_profile` | Which profile to use. |
| `--json` | off | Emit one JSON summary on stdout; human output moves to stderr. |
| `--force` | off | Ignore a held destination-table lock and take it anyway. |
| `--apps` | all resolved apps | Comma-separated AppsFlyer app ids to restrict to. |
| `--chunk-days` | each extract's own `chunk_days` | Override chunk size for this run. |
| `--max-calls` | unlimited | Stop scheduling after this many AppsFlyer API calls. |
| `--max-minutes` | unlimited | Stop scheduling after this many minutes elapsed. |
| `--allow-empty` | off | Apply an empty AppsFlyer response even where `on_empty: skip`. |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Every chunk succeeded, or was skipped by policy (quota/`--max-calls`/`--max-minutes`, the empty-response guard, a disabled extract). |
| `1` | Any chunk failed, a config/DB error occurred, a lock is held by another run, or the selector matched no enabled extract. |
| `2` | Usage error (bad CLI arguments). |

### `--json` output (`schema_version: 2`)

```json
{
  "schema_version": 2,
  "command": "run",
  "project": "my_project", "profile": "prod", "run_id": "20260922T101530Z-a1b2c3",
  "selector": "*", "exclude": null,
  "started_at": "2026-09-22T10:15:30", "finished_at": "2026-09-22T10:16:02",
  "duration_seconds": 32.1,
  "status": "success",
  "error": null,
  "aborted": null,
  "extracts": [
    {"name": "standard", "table": "appsflyer.appsflyer_geo_by_date",
     "apps": 11, "chunks": 33, "succeeded": 33, "failed": 0, "skipped": 0,
     "rows": 4210, "api_calls": 33, "api_calls_long": 0,
     "window": {"from": "2026-09-19", "to": "2026-09-22"}}
  ],
  "jobs": [
    {"extract": "standard", "app_id": "123456789", "from": "2026-09-19", "to": "2026-09-20",
     "status": "success", "rows": 120, "api_calls": 1, "http_status": 200,
     "error": null, "skip_reason": null}
  ],
  "days_rebuilt": [
    {"table": "appsflyer.appsflyer_geo_by_date", "partition": "202609",
     "days": ["2026-09-19"], "action": "replace", "kept_rows": 0, "fresh_rows": 120}
  ],
  "quota": {"account_long_used": 0, "account_long_budget": 120, "per_app": {}},
  "totals": {"chunks": 33, "succeeded": 33, "failed": 0, "skipped": 0,
             "rows": 4210, "api_calls": 33, "days_rebuilt": 4},
  "exit_code": 0
}
```

`status` is one of `success` / `failed` / `dry_run` / `error`. `aborted` is
`null` unless the run stopped early — `"auth"` (401, or an unresolved 403)
or `"clickhouse"` (a partition-rebuild failure); everything unprocessed at
that point is recorded `skipped` with the abort reason.

`days_rebuilt` is keyed by **partition**, not day (`schema_version` went
1 → 2 when `partition_granularity` became configurable): under the default
`partition_granularity: month` one entry can cover every day of a calendar
month a wave touched, not just one — `"days"` lists only *that wave's own*
days (a month partition can be rebuilt by several waves across one run).
`totals.days_rebuilt` counts partition-rebuilds, not days.

## `afly ls`

```bash
afly ls [--select SEL] [--json]
```

| Option | Default | Meaning |
|---|---|---|
| `--select` / `-s` | `*` | Selector for extracts to list. |
| `--json` | off | Emit each matched extract's full resolved config + file path. |

Offline — never reads `profiles.yml`. Exit 1 if the selector matches nothing.

## `afly validate`

```bash
afly validate [--select SEL]
```

| Option | Default | Meaning |
|---|---|---|
| `--select` / `-s` | `*` | Selector for extracts to validate. |

Loads `profiles.yml` with unresolved env vars as **warnings**, not failures
(unlike every other command). Exit 1 if any layer fails to load; warnings
never fail it.

## `afly debug`

```bash
afly debug [--profile NAME] [--deep] [--pull EXTRACT --app ID [--from D] [--to D]] [--json]
```

| Option | Default | Meaning |
|---|---|---|
| `--profile` | default profile | Which profile to check. |
| `--deep` | off | Also verify live `REPLACE`/`DROP PARTITION` mechanics on disposable tables. |
| `--pull` | none | Name of one extract to test-pull a small sample for. |
| `--app` | *(required with `--pull`)* | AppsFlyer app id to scope the sample pull to. |
| `--from` / `--to` | yesterday..yesterday | Sample window (max 2 days total). |
| `--json` | off | Emit every check result (and the pull summary, if requested) as JSON. |

Exit 1 if any check (or the sample pull) fails.

## `afly apps`

```bash
afly apps [--profile NAME] [--platform ios|android] [--json]
```

| Option | Default | Meaning |
|---|---|---|
| `--profile` | default profile | Which profile's AppsFlyer token to use. |
| `--platform` | all | Restrict the listing to one platform. |
| `--json` | off | Emit `[{id, name, platform, currency, time_zone}, ...]`. |

## `afly unlock`

```bash
afly unlock (--table DB.TABLE | --all) [--profile NAME]
```

| Option | Default | Meaning |
|---|---|---|
| `--table` | — | Clear the lock on exactly this destination table. |
| `--all` | — | Clear every active lock in the project. |
| `--profile` | default profile | Which profile's ClickHouse to connect to. |

Exactly one of `--table`/`--all` is required.

## Selectors

Used by `run`, `ls`, `validate`. Items separated by commas/whitespace, each
one of:

| Form | Matches |
|---|---|
| `*` | Every extract. |
| `tag:<t>` | Extracts whose `tags:` list contains `<t>`. |
| a glob (`fnmatch`) | The extract's `name`, or its file path relative to `extracts/`. An exact name is just a glob with no wildcards. |

`--select` items are unioned; `--exclude` (on `run`) is subtracted afterward.
Result order always follows discovery order. `run` additionally drops any
selected extract with `enabled: false` before planning (logged, not an
error).
