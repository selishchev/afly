# Config reference

Every field, its type, and its default across `afly_project.yml`,
`profiles.yml`, and one `extracts/*.yml`. Every model rejects unknown keys
(`extra: forbid`) — a typo'd field fails to load loudly rather than being
silently ignored.

## Project (`afly_project.yml`)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | string, `^[A-Za-z0-9_-]+$` | *(required)* | Project name. |
| `version` | string | `"1.0"` | Config format version (informational). |
| `default_profile` | string | *(required)* | Profile used when `--profile` isn't passed. |
| `paths.extracts` | string | `"extracts"` | Directory `extracts/*.yml` are discovered from. |
| `tables.loads` | string | `"_afly_loads"` | Idempotency-ledger table name. |
| `tables.locks` | string | `"_afly_locks"` | Lock table name. |
| `defaults.start_date` | date | *(none)* | Fallback `start_date` for extracts that don't set their own. Required somewhere. |
| `defaults.lookback_days` | int, ≥0 | `3` | Fallback `lookback_days`. |
| `defaults.chunk_days` | int, 1–90 | `2` | Fallback `chunk_days`. |
| `defaults.include_current_day` | bool | `true` | Fallback `include_current_day`. |
| `defaults.timezone` | string \| null | `null` | Fallback `timezone` (AppsFlyer account timezone if unset). |
| `defaults.currency` | `preferred` \| `USD` | `preferred` | Fallback `currency`. **AppsFlyer ignores this for the aggregate geo reports afly pulls** — it always returns each app's own currency; afly stores whatever currency the response actually came in as the destination `currency` column ([Tables reference](tables.md)) rather than converting. |
| `defaults.on_empty` | `skip` \| `replace` | `skip` | Fallback `on_empty`. |
| `defaults.keep_unknown_columns` | bool | `false` | Fallback `keep_unknown_columns`. |
| `defaults.partition_granularity` | `month` \| `day` | `month` | Fallback `partition_granularity` — the destination's `PARTITION BY` grain. Extracts sharing one `table:` must resolve to the same value (checked at load time, see [Idempotency guide](../guides/idempotency.md)). |
| `quota.short_call_interval_seconds` | int, ≥0 | `65` | Per-`(app, report_type)` throttle (not 60 — AppsFlyer's own per-minute window still 403s on exactly-60s spacing). |
| `quota.long_call_min_days` | int, ≥1 | `3` | Chunk day-span at/above which a call counts as "long". |
| `quota.account_long_calls_per_day` | int, ≥0 | `120` | Account-wide daily long-call budget. |
| `quota.app_long_calls_per_day` | int, ≥0 | `24` | Per-app daily long-call budget. |
| `quota.reserve_long_calls` | int, ≥0 | `0` | Headroom subtracted from both budgets above. |
| `quota.min_gap_seconds` | float, ≥0 | `0.5` | Global minimum gap between any two AppsFlyer calls. |
| `quota.max_retries` | int, ≥0 | `5` | Retry attempts before giving up on a call. |
| `quota.max_waves_in_flight` | int, ≥1 | `8` | How many plan waves the scheduler may draw jobs from at once. `1` reproduces the old strict one-wave-at-a-time order; see [Quotas & scheduling](../guides/quotas.md#wave-lookahead). |
| `lock_timeout_seconds` | int, 60–86400 | `7200` | Age at which a `running` lock is treated as stale. |
| `error_alerting.enabled` | bool | `false` | Whether a run-failure alert can fire. |
| `error_alerting.channels` | list[string] | `[]` | Channel names, looked up in `profiles.yml`'s `alert_channels`. |
| `error_alerting.mentions` | list[string] | `[]` | Passed through to the alert payload. |

## Profiles (`profiles.yml`)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `default_profile` | string \| null | `null` | Profile used when `afly_project.yml`'s isn't overridden here. |
| `profiles.<name>.appsflyer.token` | string | *(required)* | API V2 Bearer token. |
| `profiles.<name>.appsflyer.base_url` | string | `https://hq1.appsflyer.com` | API base URL. |
| `profiles.<name>.appsflyer.timeout_seconds` | int, ≥1 | `120` | HTTP timeout. |
| `profiles.<name>.appsflyer.user_agent` | string \| null | `afly/<version>` | Request `User-Agent`. |
| `profiles.<name>.clickhouse.host` | string | *(required)* | ClickHouse host. |
| `profiles.<name>.clickhouse.protocol` | `native` \| `http` | `native` | Transport — `native` uses `clickhouse-driver` (TCP), `http` uses `clickhouse-connect`. |
| `profiles.<name>.clickhouse.port` | int \| null | `null` (protocol default) | Port. Unset follows `protocol`: native 9000 (9440 if `secure`), http 8123 (8443 if `secure`). Set explicitly to override. |
| `profiles.<name>.clickhouse.user` | string | `default` | ClickHouse user. |
| `profiles.<name>.clickhouse.password` | string | `""` | ClickHouse password. |
| `profiles.<name>.clickhouse.database` | string | *(required)* | Database destination tables live in. |
| `profiles.<name>.clickhouse.internal_database` | string \| null | same as `database` | Database `_afly_loads`/`_afly_locks` live in. |
| `profiles.<name>.clickhouse.staging_database` | string \| null | the destination's database | Database for the transient `…__afly_staging` tables of the partition rebuild (named `<db>__<table>__afly_staging` when it differs from the destination's). Set it when the destination database is mirrored automatically — e.g. a Distributed-wrapper sync over `raw` — so staging tables are never published. |
| `profiles.<name>.clickhouse.secure` | bool | `false` | TLS. |
| `profiles.<name>.clickhouse.verify` | bool | `true` | TLS certificate verification. |
| `profiles.<name>.clickhouse.settings` | dict | `{}` | Passed through to `clickhouse-driver`. |
| `profiles.<name>.clickhouse.connect_timeout` | int, ≥1 | `10` | Connection timeout, seconds. |
| `profiles.<name>.clickhouse.send_receive_timeout` | int, ≥1 | `600` | Query timeout, seconds. |
| `alert_channels.<name>.type` | `mattermost` \| `slack` \| `webhook` | *(required)* | Channel type. |
| `alert_channels.<name>.webhook_url` | string | *(required)* | Delivery URL. |
| `alert_channels.<name>.channel` | string \| null | `null` | Channel/room override. |
| `alert_channels.<name>.username` | string | `afly` | Bot display name. |
| `alert_channels.<name>.icon_emoji` | string \| null | `null` | Bot icon. |
| `alert_channels.<name>.timeout` | int, ≥1 | `10` | POST timeout, seconds. |

### Environment interpolation

Any string value may use `${VAR_NAME}` or `{{ env_var('VAR_NAME') }}`,
resolved from the process environment at load time. See
[Configuration guide](../guides/configuration.md#environment-interpolation).

## Extract (`extracts/<name>.yml`)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | string, `^[a-z0-9_]+$` | *(required)* | Unique across the project — selector + idempotency key. |
| `description` | string \| null | `null` | Free text. |
| `enabled` | bool | `true` | `false` skips this extract (not an error). |
| `report_type` | `geo_by_date_report` \| `partners_by_date_report` \| `daily_report` | *(required)* | AppsFlyer report to pull. (`partners_report`/`geo_report` are rejected — no `Date` column.) |
| `category` | `standard` \| `facebook` \| `organic` | `standard` | AppsFlyer `category` query param. |
| `media_source` | string \| null | `null` | Scope the pull to one AppsFlyer media source. |
| `exclude_media_sources` | list[string] | `[]` | Drop these CSV `Media Source` values. Mutually exclusive with `media_source`. |
| `reattr` | bool | `false` | Pull the retargeting/reattribution dataset. |
| `attribution_touch_type` | `click` \| `impression` \| null | `null` | `impression` = view-through. |
| `timezone` | string \| null | project `defaults.timezone` | Override. |
| `currency` | `preferred` \| `USD` \| null | project `defaults.currency` | Override. Same caveat as `defaults.currency` above — AppsFlyer ignores it for these reports. |
| `apps` | list[string] \| null | `null` (whole account) | Explicit app ids. |
| `exclude_apps` | list[string] | `[]` | Subtracted from `apps`/the account list. |
| `platforms` | list[string] \| null | `null` | Filter when `apps` is unset (e.g. `["ios"]`). |
| `start_date` | date \| null | project `defaults.start_date` | Earliest day ever pulled. Required somewhere. |
| `lookback_days` | int, ≥0 \| null | project `defaults.lookback_days` | Override. |
| `include_current_day` | bool \| null | project `defaults.include_current_day` | Override. |
| `chunk_days` | int, 1–90 \| null | project `defaults.chunk_days` | Override. |
| `table` | string | *(required)* | `table` (profile database) or `db.table`. |
| `on_empty` | `skip` \| `replace` \| null | project `defaults.on_empty` | Override. |
| `keep_unknown_columns` | bool \| null | project `defaults.keep_unknown_columns` | Override. |
| `partition_granularity` | `month` \| `day` \| null | project `defaults.partition_granularity` | Override. Extracts writing the same `table:` must agree — see the validation rules below. |
| `extra_params` | dict[str, str] | `{}` | Extra AppsFlyer query params (never `from`/`to`). |
| `tags` | list[string] | `[]` | Used by `--select tag:<t>`. |

### Validation rules (fail the load)

- `report_type` in `{partners_report, geo_report}`.
- `category: organic` with `media_source` set.
- `media_source` set together with non-empty `exclude_media_sources`.
- `extra_params` containing `from`/`to`.
- `table` with more than one `.`, or an empty database/table segment.
- Two extracts with the same `name`.
- Two extracts writing the same `table:` that resolve (after defaults) to
  different `partition_granularity` values — a destination table has exactly
  one `PARTITION BY`.

### Validation warnings (`afly validate`)

- `chunk_days >= quota.long_call_min_days` — counts against the daily budget.
- A likely double-counted media source between an unfiltered extract and a
  scoped sibling writing the same `table`. See
  [Extracts guide](../guides/extracts.md#the-ownership-convention-avoiding-double-counted-spend).
