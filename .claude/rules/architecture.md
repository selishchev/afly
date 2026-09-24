# Architecture

Internals and design rationale for afly itself. For the *user-facing*
reference (what a project author needs), see `afly/cli/assets/claude/rules/`
— that's what `afly init-claude` ships into a project. For contributor
mechanics (dev setup, tests, extending), see `./contributing.md`.

## Module map

```
afly/
├── cli/
│   ├── main.py                 # click group; every subcommand lazy-imports its impl
│   ├── _output.py               # echo_tree/echo_done/echo_error/echo_warning house style
│   ├── _project.py              # find_project_root + load_context/load_project_only
│   ├── commands/                # one module per subcommand (init, init-claude, run, ls,
│   │                             #   validate, debug, apps, unlock)
│   └── assets/claude/           # shipped `afly init-claude` context (CLAUDE.section.md,
│                                 #   rules/, skills/) — see the "assets" section below
├── config/
│   ├── project_config.py        # afly_project.yml (ProjectConfig, ExtractDefaults, QuotaConfig, ...)
│   ├── profile.py                # profiles.yml (ProfilesConfig, env-interpolated)
│   ├── extract_config.py         # extracts/*.yml (ExtractConfig, with_defaults, warnings_for)
│   ├── discovery.py               # find + parse + default-merge every extracts/*.yml
│   └── selectors.py               # --select/--exclude grammar
├── appsflyer/
│   ├── client.py                  # thin requests wrapper, auth headers, api_calls counter
│   ├── errors.py                  # AppsFlyerError hierarchy + classify_response/build_error
│   ├── retry.py                   # RetryPolicy — the one place backoff is decided
│   ├── pull_api.py                # PullRequestSpec + build_pull_request + fetch_report
│   └── mng_api.py                 # app list (paginated), used by `afly apps` + apps-for-extract
├── csvmap/
│   ├── headers.py                  # KNOWN_HEADERS, event-triple regex, detect_format
│   ├── values.py                    # to_str/to_uint/to_float/to_date, null-token handling
│   └── parser.py                    # parse_report: CSV -> rows shaped like afly.schema
├── schema.py                        # DESTINATION_COLUMNS/ORDER_BY/PARTITION_BY — single source
│                                      #   of truth the parser AND the DDL generator both import
├── database/
│   ├── clickhouse.py                 # ClickHouseManager: wraps clickhouse_driver.Client
│   ├── ddl.py                         # destination_ddl, check_schema/ensure_destination
│   ├── writer.py                      # PartitionRebuilder — the idempotent write path
│   ├── loads.py                        # _afly_loads repo (ledger + watermark + quota spend)
│   ├── locks.py                        # _afly_locks repo (acquire/release/clear, stale auto-recovery)
│   ├── tables.py                       # DDL for _afly_loads/_afly_locks themselves
│   └── checks.py                       # afly debug's connectivity + --deep live-server probes
├── run/
│   ├── options.py                      # RunOptions — parsed CLI options, frozen
│   ├── windows.py                       # pure date math: compute_window, chunk_window
│   ├── planner.py                       # ChunkJob/Plan — resolve extracts into concrete jobs
│   ├── scheduler.py                      # QuotaScheduler — hands out jobs from up to max_waves_in_flight waves, in rate-limit order
│   ├── executor.py + _fetch.py/_rebuild.py/_job_result.py/_wave_window.py   # drives a Plan's waves (lookahead dispatch, strict-order rebuild)
│   ├── _setup.py                          # per-run lock + destination + staging-table setup
│   ├── _apps.py                            # resolve which app ids each extract targets
│   ├── _alert.py                            # once-per-run failure alert dispatch
│   ├── summary.py                           # RunSummary — the --json schema_version:1 contract
│   └── runner.py                            # run_pipeline — wires all of the above together
├── alerting/webhook.py                      # Mattermost/Slack/webhook failure-alert POST
└── utils/                                    # datetime (naive-UTC), env interpolation, table naming
```

## Data flow (one `afly run`)

`cli/commands/run.py` parses options → `run/runner.py:run_pipeline` loads the
project/profile, resolves each selected extract's app list
(`run/_apps.py`), and calls `run/planner.py:build_plan` — which, per
`(extract, app)` **pair**, computes a window (`run/windows.py`) from the
watermark (`database/loads.py`) and splits it into epoch-aligned chunks. Jobs
group into **waves** by chunk index. For each wave in order: `run/scheduler.py`
hands out jobs respecting AppsFlyer's rate limits; each job fetches
(`appsflyer/pull_api.py`) and parses (`csvmap/parser.py`) one chunk; once the
whole wave has been fetched, `run/_rebuild.py` calls
`database/writer.py:PartitionRebuilder.rebuild_day` once per touched
`(table, day)`. Every chunk attempt is logged to `_afly_loads`
(`database/loads.py`) before and after; a whole-run failure triggers
`run/_alert.py` if configured. `run/summary.py:RunSummary` accumulates
everything for both the human and `--json` renderings.

## Design decisions

### Why one destination schema, not per-report-type tables

`afly.schema.DESTINATION_COLUMNS` is a superset covering every supported
`report_type`/`category` combination (Facebook's adset/adgroup columns
included) rather than a schema per shape. This lets several extracts with
different scopes (standard/Facebook/Yandex) share one destination table —
the scaffold's default — without a union/view layer, at the cost of some
unused columns for a standard-shaped pull. Both `csvmap/parser.py` and
`database/ddl.py` import the same module, so the two can never drift apart.

### Why day-partition rebuild, not `DELETE`+`INSERT` or `ReplacingMergeTree`

ClickHouse 22.11 has no cheap `DELETE FROM` and no synchronous
`ReplacingMergeTree` collapse a plain `SELECT` can rely on without `FINAL`.
`database/writer.py:PartitionRebuilder` instead rebuilds a whole day's
partition in a staging table (old rows minus the pairs this wave is
authoritative for, plus fresh rows) and atomically swaps it in with
`ALTER TABLE ... REPLACE PARTITION`. Full protocol and the crash-safety
argument: the shipped `idempotency.md` (`afly/cli/assets/claude/rules/`) —
authoritative and user-facing, not duplicated here.

**The one finding worth repeating in this file because it's easy to get
backwards**: verified live on ClickHouse 22.11, `REPLACE PARTITION ID 'X'
FROM staging` when `staging` has **no part at all** for partition X does
**not** raise — it silently **empties** the destination's partition X. The
writer therefore branches on whether staging ended up non-empty
(`REPLACE` when non-empty, `DROP PARTITION` when empty and the destination
still has that partition, no-op otherwise) rather than always calling
`REPLACE`. `afly debug --deep` reproduces this finding on disposable tables
so it stays verifiable against whatever server version a user is actually
running, rather than trusted as a fact about ClickHouse in general.

### Why `_afly_loads`/`_afly_locks` are append-only

Same 22.11 constraint: no cheap in-place update. `loads.py`/`locks.py` never
`UPDATE` a row — `start_chunk`/`finish_chunk` write two independent rows per
attempt, and lock acquire/release write a new row on top (`_afly_locks` is a
`ReplacingMergeTree`, always read through `FINAL`). This is also a better
audit log than an update would be: a chunk that crashed mid-flight leaves a
visible `running` row forever instead of vanishing.

### Why the AppsFlyer client never interprets status codes itself

`appsflyer/client.py` only owns transport (auth headers, timeout, redirects,
turning a network failure into `TransientError`). `appsflyer/errors.py` is
the **one** place an HTTP response becomes a typed error
(`classify_response`/`build_error`) — `pull_api.py` and `mng_api.py` both
funnel through it, so retry/backoff logic (`retry.py`) and future callers
never re-derive "is this a 403-with-quota-marker or a bare 403" from scratch.

### Why the run pipeline is dependency-injected (`RunDeps`)

`run/runner.py:RunDeps` holds every external dependency (config loading, the
ClickHouse manager factory, the AppsFlyer client factory, wall/monotonic
clock, sleep, the alert sender) as overridable fields. This is what makes the
scheduler/executor/planner unit-testable under a fake clock with zero real
sleeps and zero real HTTP/ClickHouse calls — the integration suite
(`clickhouse-server:22.11` via testcontainers) is what exercises the real
driver and the real partition-swap mechanics end to end.

### Why naive UTC everywhere

AppsFlyer's reports are date-bucketed with no timezone in the response, and
ClickHouse's plain `Date`/`DateTime` columns are timezone-naive. Mixing in an
aware `datetime` anywhere risks an off-by-some-hours bug that only shows up
near a boundary. `afly.utils.datetime_utils` is the one place "now"/"today"
are read from, and `database/clickhouse.py` strips tzinfo off every
`DateTime64(..., 'UTC')` value the driver returns before it reaches any
caller — so the naive-UTC contract holds without every call site having to
remember to strip it.

## The `afly init-claude` assets

`afly/cli/assets/claude/` (shipped in the wheel via
`[tool.setuptools.package-data]` and `MANIFEST.in`) is exactly what a
freshly-run `afly init-claude` writes into a project — see
`afly/cli/commands/init_claude.py`. These are user-facing docs: keep them in
sync with real CLI/config behavior whenever either changes (see the release
checklist in `./contributing.md`), and extend
`tests/unit/test_init_claude.py`'s file-set assertions whenever a rule or
skill is added or removed.
