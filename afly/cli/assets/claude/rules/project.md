# afly — project & profile config

Every afly project has two files at its root plus an `extracts/` directory
(field-level extract reference: `extracts.md`).

## `afly_project.yml`

Committed to git — no credentials live here. `extra: forbid` on every model:
a typo'd key fails loudly at load time instead of being silently ignored.

```yaml
name: my_project                  # required, pattern ^[A-Za-z0-9_-]+$
version: "1.0"                    # default "1.0"
default_profile: prod             # required — must name a profile in profiles.yml

paths:
  extracts: extracts              # default "extracts"

tables:
  loads: _afly_loads              # default
  locks: _afly_locks              # default

defaults:                         # project-wide fallback for optional extract fields
  start_date: 2026-01-01          # no default — required somewhere (project or extract)
  lookback_days: 3                # default 3, >= 0
  chunk_days: 2                   # default 2, 1..90 — see quotas.md for why 2
  include_current_day: true       # default true
  timezone: null                  # default null (AppsFlyer account timezone)
  currency: preferred             # "preferred" | "USD", default "preferred" — NOTE: AppsFlyer
                                   # ignores this for these reports; it returns each app's own
                                   # currency regardless, recorded in the destination `currency` column
  on_empty: skip                  # "skip" | "replace", default "skip"
  keep_unknown_columns: false     # default false
  partition_granularity: month    # "month" | "day", default "month" — destination PARTITION BY
                                   # (toYYYYMM(date) vs toYYYYMMDD(date)); see idempotency.md
  exclude_apps: []                # default [] — app ids dropped from EVERY extract; UNIONED
                                   # (not overridden) into each extract's own exclude_apps:

quota:                            # AppsFlyer Pull API budget afly enforces on itself
  short_call_interval_seconds: 65 # default 65 — per-(app,report_type) throttle (not 60: AppsFlyer's
                                   # own per-minute window still 403s on exactly-60s spacing)
  long_call_min_days: 3           # default 3 — chunks >= this many days are "long"
  account_long_calls_per_day: 120 # default 120
  app_long_calls_per_day: 24      # default 24
  reserve_long_calls: 0           # default 0 — headroom subtracted from both budgets above
  min_gap_seconds: 0.5            # default 0.5 — global minimum gap between any two calls
  max_retries: 5                  # default 5
  transient_base_wait_seconds: 30 # default 30 — 5xx/network backoff base; doubles per attempt
  transient_max_wait_seconds: 600 # default 600 — cap on the nominal (pre-jitter) transient wait
  retry_jitter: 0.25              # default 0.25 — +0..25% random spread on every retry/deferral
                                   # wait (never below nominal); see quotas.md's Retry/backoff
  max_waves_in_flight: 8          # default 8 — plan waves the scheduler may draw jobs from at
                                   # once; 1 = old strict one-wave-at-a-time order — see quotas.md

lock_timeout_seconds: 7200        # default 7200 (2h), 60..86400 — stale-lock auto-recovery window

error_alerting:
  enabled: false                  # default false
  channels: [ops_mattermost]      # names must exist in profiles.yml's alert_channels
  mentions: []                    # default []
```

`defaults` fields fall through to each extract via
`ExtractConfig.with_defaults()` — an extract only overrides what makes it
different (see `extracts.md`). `defaults.start_date` has **no built-in
default**: it must be set either here or on every extract, or that extract
fails to load with a named error. `defaults.partition_granularity` has an
extra cross-extract rule `start_date` doesn't: extracts sharing one `table:`
must resolve to the same value, or the whole project fails to load
(`ConfigError`, not a warning) — a destination table has exactly one
`PARTITION BY`. See `idempotency.md` for the write-path mechanics.

## `profiles.yml`

**Never commit a literal credential here.** Every secret-bearing field is
meant to hold `${VAR}` or `{{ env_var('VAR') }}`, resolved from the process
environment (typically via a `.env` file + `set -a; source .env; set +a`) at
load time. An unresolved placeholder in a credential field is a **hard error**
for every command that actually connects (`run`, `debug`, `apps`, `unlock`) —
better to fail naming the missing variable than to send a literal
`${CLICKHOUSE_PASSWORD}` string as a password. `afly validate` is the one
exception: it loads with `strict_env=False` and turns the same unresolved
names into warnings, so it can pass in CI before secrets exist.

```yaml
default_profile: prod             # profiles.yml's own default, distinct from
                                   # afly_project.yml's default_profile — keep them equal

profiles:
  prod:
    appsflyer:
      token: "{{ env_var('APPSFLYER_TOKEN') }}"   # required, API V2 Bearer token
      base_url: https://hq1.appsflyer.com          # default; regional accounts may differ
      timeout_seconds: 120                          # default 120
      user_agent: null                              # default "afly/<version>"
    clickhouse:
      host: "{{ env_var('CLICKHOUSE_HOST') }}"      # required
      protocol: native                              # default "native" (clickhouse-driver, TCP) | "http" (clickhouse-connect, HTTP)
      port: null                                    # default: protocol's own port — native 9000/9440, http 8123/8443 (secure variants); set to override
      user: "{{ env_var('CLICKHOUSE_USER') }}"       # default "default"
      password: "{{ env_var('CLICKHOUSE_PASSWORD') }}"  # default ""
      database: appsflyer                            # required — destination tables live here
      internal_database: null                        # default: same as database
      staging_database: afly     # optional: transient staging tables outside a mirrored destination db
      secure: false                                   # default false (TLS)
      verify: true                                    # default true (TLS cert verification)
      settings: {}                                    # default {} — passed through to the client (clickhouse-driver or clickhouse-connect)
      connect_timeout: 10                             # default 10
      send_receive_timeout: 600                       # default 600

alert_channels:
  ops_mattermost:
    type: mattermost                                 # "mattermost" | "slack" | "webhook"
    webhook_url: "{{ env_var('MATTERMOST_WEBHOOK_URL') }}"  # required
    channel: null                                    # optional override
    username: afly                                   # default "afly"
    icon_emoji: null                                 # optional
    timeout: 10                                       # default 10
    run_url: "${PREFECT_UI_BASE_URL}/runs/flow-run/${PREFECT__FLOW_RUN_ID}"  # default "" — link to
                                   # this run in your orchestrator; empty/unresolved after env
                                   # interpolation -> silently omitted from the alert, never an error
```

- **`internal_database`** is where `_afly_loads`/`_afly_locks` live — keep it
  separate from `database` only if you want afly's bookkeeping isolated from
  the destination tables; the common case leaves it unset (same database).
- **`protocol`/`port`** — same warehouse, two wire protocols: `native`
  (`clickhouse-driver`, TCP) or `http` (`clickhouse-connect`, HTTP). Use
  `http` when the native port is firewalled off or doesn't answer the
  handshake from where afly runs; both protocols hit the same tables with
  identical results. `port` unset follows `protocol`'s own default — set it
  only to override.
- A project can define several `profiles:` (e.g. `prod`/`dev` against
  different ClickHouse clusters or AppsFlyer accounts) and select one per run
  with `afly run --profile <name>`.
- **`alert_channels`** are referenced by name from `error_alerting.channels`
  in `afly_project.yml` — this is a *run-failure* alert, fired at most once
  per run on any exit-1 outcome that happens after the project loaded (a
  failed/aborted run, a held lock, a schema mismatch, a bad extract config,
  an empty selector match, an AppsFlyer app-list error), never for a
  quota/policy skip or `--dry-run`. `type: mattermost`/`slack` post to the
  webhook's native "attachments" shape — mentions (normalized to exactly one
  leading `@` each) are the attachment's last line, with the resolved
  `run_url` (if any) on the line just above; `type: webhook` posts a plain
  `{title, text, project, run_id, run_url, mentions}` JSON body. See
  `docs/guides/alerting.md` for the exact trigger list and payload shape.

## Env interpolation syntax

Both of these are recognized, anywhere a string value appears in
`profiles.yml`:

```yaml
password: "${CLICKHOUSE_PASSWORD}"                  # shell-style
password: "{{ env_var('CLICKHOUSE_PASSWORD') }}"    # dbt-style
```

A variable that is set but empty resolves to an empty string (valid, e.g. a
blank ClickHouse password for `user: default`); a variable that is **not**
set at all leaves the placeholder text in place, which is what triggers the
strict-env error / validate warning above.
