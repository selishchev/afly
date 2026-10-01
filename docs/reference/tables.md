# Tables reference

Column reference for the destination table shape every extract writes, plus
afly's two internal bookkeeping tables.

## Destination table

Created automatically on first write (`CREATE TABLE IF NOT EXISTS`) if it
doesn't already exist; an existing table is checked column-by-column and
afly **refuses to write** to one that's missing a column or has a mismatched
type — see [Idempotency guide](../guides/idempotency.md). Several extracts
commonly share one destination table (the `standard`/`facebook`/`yandex`
scaffold all write `appsflyer_geo_by_date`) — see
[Extracts](../guides/extracts.md#the-ownership-convention-avoiding-double-counted-spend).

`ENGINE = MergeTree`, `PARTITION BY toYYYYMM(date)` (default —
`partition_granularity: month`; `toYYYYMMDD(date)` under `day`, see
[Config reference](config.md)),
`ORDER BY (app_id, date, media_source, campaign, adset_id, adgroup_id, country)`.
No dedup — plain `MergeTree`, not `ReplacingMergeTree`: two rows identical on
every column both land and both count (see
[Idempotency guide](../guides/idempotency.md)).

| Column | Type | Meaning |
|---|---|---|
| `app_id` | `LowCardinality(String)` | AppsFlyer app id. |
| `date` | `Date` | Report day (the partition key). |
| `report_type` | `LowCardinality(String)` | The extract's `report_type`, stamped from context. |
| `category` | `LowCardinality(String)` | The extract's `category`, stamped from context. |
| `is_retargeting` | `UInt8` | 1 if the extract's `reattr: true`. |
| `agency` | `String` | AppsFlyer agency/PMD. |
| `media_source` | `LowCardinality(String)` | Media source (network). |
| `campaign` | `String` | Campaign name — from `Campaign (c)`, or from `campaign_name` on a Facebook-shaped row. |
| `campaign_name` | `String` | Facebook-shaped rows only; `""` otherwise. |
| `campaign_id` | `String` | Facebook-shaped rows only; `""` otherwise. |
| `adset` | `String` | Facebook-shaped rows only; `""` otherwise. |
| `adset_id` | `String` | Facebook-shaped rows only; `""` otherwise. |
| `adgroup` | `String` | Facebook-shaped rows only; `""` otherwise. |
| `adgroup_id` | `String` | Facebook-shaped rows only; `""` otherwise. |
| `country` | `LowCardinality(String)` | Country. |
| `currency` | `LowCardinality(String)` | The currency every money column of this row (`total_revenue`, `total_cost`, `arpu`, `average_ecpi`, `event_sales`) is denominated in. AppsFlyer reports each app's aggregate geo report in **that app's own currency** and ignores the `currency=USD` request param for these reports — so this is usually the app's currency (`ISO 4217`, e.g. `USD`, `EUR`), detected from the CSV's own `Sales in <CUR>` headers when present, else the AppsFlyer app-list API's stated currency, else `""` if neither is known. See [Formats guide](../guides/formats-and-facebook-split.md#currency). |
| `impressions` | `Nullable(UInt64)` | |
| `clicks` | `Nullable(UInt64)` | |
| `ctr` | `Nullable(Float64)` | Whole-percent, not 0–1 (see [Formats guide](../guides/formats-and-facebook-split.md)). |
| `installs` | `Nullable(UInt64)` | |
| `conversion_rate` | `Nullable(Float64)` | Whole-percent. |
| `sessions` | `Nullable(UInt64)` | |
| `loyal_users` | `Nullable(UInt64)` | |
| `loyal_users_rate` | `Nullable(Float64)` | Whole-percent. |
| `total_revenue` | `Nullable(Float64)` | |
| `total_cost` | `Nullable(Float64)` | |
| `roi` | `Nullable(Float64)` | |
| `arpu` | `Nullable(Float64)` | |
| `average_ecpi` | `Nullable(Float64)` | |
| `event_unique_users` | `Map(String, UInt64)` | Keyed by in-app event name. |
| `event_counter` | `Map(String, UInt64)` | Keyed by in-app event name. |
| `event_sales` | `Map(String, Float64)` | Keyed by in-app event name. Denominated in `currency` (see above) — despite the AppsFlyer CSV header being literally `"<event> (Sales in USD)"`, AppsFlyer returns the app's own currency here regardless, so this column is never USD-only. |
| `extra` | `Map(String, String)` | Unknown CSV columns, only populated when `keep_unknown_columns: true`. |
| `_extract` | `LowCardinality(String)` | The extract `name` that wrote this row — the ownership tag the write path keys on. |
| `_run_id` | `String` | The `afly run` invocation that wrote this row. |
| `_loaded_at` | `DateTime64(3, 'UTC')` | When this row was written. |

## Staging table (`<table>__afly_staging`)

A byte-for-byte schema copy of the destination table, named
`<table>__afly_staging` (or `<db>__<table>__afly_staging` when
`staging_database` differs from the destination's database — see
[Config reference](config.md)), that `PartitionRebuilder` uses to stage a
partition's new contents before the atomic `REPLACE`/`DROP PARTITION` swap
(see [Idempotency guide](../guides/idempotency.md)). It is **(re)created at
the start of every `afly run`** (dropped first, then recreated from the
destination's current schema, so a stale copy from before an `ALTER TABLE
... ADD COLUMN` can never desync) **and dropped again when the run ends** —
so it is never visible between runs, and never holds data a reader could
observe mid-swap.

## `_afly_loads` — the idempotency ledger

Append-only: two rows per chunk-pull attempt (`running` at start,
`success`/`failed`/`skipped` at finish) — never an in-place update.
`ENGINE = MergeTree`, `PARTITION BY toYYYYMM(started_at)`,
`ORDER BY (extract, app_id, from_date, started_at)`.

| Column | Type | Meaning |
|---|---|---|
| `run_id` | `String` | The `afly run` invocation. |
| `extract` | `LowCardinality(String)` | Extract name. |
| `app_id` | `LowCardinality(String)` | AppsFlyer app id. |
| `report_type` | `LowCardinality(String)` | |
| `from_date` / `to_date` | `Date` | The chunk's pull window. |
| `chunk_days` | `UInt8` | Actual day-span of this chunk. |
| `is_long` | `UInt8` | 1 if this chunk counted against the daily long-call budget. |
| `status` | `LowCardinality(String)` | `running` \| `success` \| `failed` \| `skipped`. |
| `rows` | `UInt64` | Rows written (0 for `failed`/`skipped`). |
| `api_calls` | `UInt16` | AppsFlyer calls spent on this attempt (across retries). |
| `http_status` | `Nullable(UInt16)` | Last HTTP status, if any. |
| `started_at` / `finished_at` | `DateTime64(3, 'UTC')` / `Nullable(...)` | Timing. |
| `duration_ms` | `Nullable(UInt32)` | |
| `error` | `Nullable(String)` | Set for `failed`. |
| `skip_reason` | `Nullable(String)` | Set for `skipped` (quota, empty-response guard, `--max-calls`/`--max-minutes`, …). |
| `afly_version` | `String` | The afly version that wrote the row. |

```sql
-- Watermark per (extract, app) — how far each pair has successfully loaded
SELECT extract, app_id, max(to_date) AS watermark
FROM _afly_loads
WHERE status = 'success'
GROUP BY extract, app_id;
```

## `_afly_locks` — one lock per destination table

`ENGINE = ReplacingMergeTree(updated_at)`, `ORDER BY lock_key` — always read
through `FINAL` (merges aren't synchronous). A `running` row is *released* by
inserting a `released` row on top, never an in-place update.

| Column | Type | Meaning |
|---|---|---|
| `lock_key` | `String` | `table:<db>.<table>`. |
| `run_id` | `String` | The holding run. |
| `status` | `LowCardinality(String)` | `running` \| `released`. |
| `owner` | `String` | `hostname:pid` of the holding process. |
| `started_at` / `updated_at` | `DateTime64(3, 'UTC')` | |
| `timeout_seconds` | `UInt32` | Staleness window (from `afly_project.yml`'s `lock_timeout_seconds`). |

```sql
-- Currently held locks, with computed staleness
SELECT lock_key, run_id, owner, started_at,
       dateDiff('second', started_at, now()) AS age_seconds,
       age_seconds > timeout_seconds AS stale
FROM _afly_locks FINAL
WHERE status = 'running';
```

Both internal tables live in the profile's `internal_database` (defaults to
the same database as the destination tables) and are created automatically
on first `afly run`.
