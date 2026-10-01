# afly — idempotency (the partition rebuild write path)

## Why not `DELETE` + `INSERT`, or `ReplacingMergeTree`

ClickHouse 22.11 (afly's baseline) has neither a cheap `DELETE FROM` nor
`ReplacingMergeTree(ver, is_deleted)` collapse-on-write semantics readers can
rely on without `FINAL` (expensive at scale). "Re-run a chunk without
duplicating rows" is instead implemented as an **atomic partition swap**: for
each partition a wave touched, rebuild the whole partition in a staging
table (old rows minus what's being replaced, plus the fresh rows) and swap it
in with `REPLACE PARTITION` — an all-or-nothing operation at the partition
level, with no `FINAL` needed by any reader, ever.

## Configurable partition granularity (`partition_granularity`)

The destination's `PARTITION BY` is `toYYYYMM(date)` (**`month`**, the
default) or `toYYYYMMDD(date)` (**`day`**) — set via `afly_project.yml`'s
`defaults.partition_granularity`, overridable per extract, resolved by
`afly.database.ddl.destination_ddl`/`partition_id`. `month` is the default:
a real destination table measured ~1.7MB/41k rows per 23 days — daily
partitions mean hundreds of tiny parts a year, and ClickHouse's own guidance
is coarse (monthly) partitions.

Two extracts writing the **same** `table:` must resolve to the same
granularity — checked at `load_extracts` time
(`afly.config.extract_config.granularity_conflicts_for`), a hard
`ConfigError`, not a warning: a destination table has exactly one
`PARTITION BY`. An existing table whose live `system.tables.partition_key`
doesn't match the configured granularity is a `SchemaMismatchError` from
`ensure_destination`/`check_schema` — **afly never re-partitions a table**
(ClickHouse has no in-place `ALTER ... MODIFY PARTITION BY`; changing
granularity means recreating the table). Pre-release, no migration path is
provided.

## The protocol, step by step (`PartitionRebuilder.rebuild_partition`)

For one `(table, partition_id)`, given the set of `(day, extract, app_id)`
**coverage** triples this wave is authoritative for:

1. `TRUNCATE` the destination's staging table (`<table>__afly_staging`,
   (re)created once at the **start** of the run as `CREATE TABLE ... AS
   <destination>` — always the destination's *current* schema, so a stale
   staging table from before an `ALTER TABLE ... ADD COLUMN` can never
   desync — and `DROP`ped again at the **end** of the run, so it's never
   visible between runs).
2. `INSERT INTO staging SELECT * FROM destination WHERE <partition expr> =
   <partition_id> AND (date, _extract, app_id) NOT IN <coverage>` — every
   existing row of that partition **except** the `(day, extract, app_id)`
   triples this wave owns. A triple *not* in coverage (e.g. a different day
   of the same month partition, or an extract that wasn't part of this run)
   keeps whatever the destination already had — this is what lets a
   month-wide partition be rebuilt safely by several consecutive waves in
   one run, each wave passing only its own days (see
   `afly.run._rebuild.rebuild_wave`).
3. `INSERT INTO staging VALUES <fresh rows>` — the rows this wave actually
   fetched, for the covered triples.
4. Branch on whether staging ended up non-empty:
   - **non-empty** → `ALTER TABLE destination REPLACE PARTITION ID
     '<partition_id>' FROM staging`.
   - **empty, and the destination still has that partition** → `ALTER TABLE
     destination DROP PARTITION ID '<partition_id>'` (every covered triple's
     rows are gone and nothing else remains in the partition).
   - **empty, destination has no such partition either** → nothing to do.
5. `TRUNCATE` staging again, in a `finally` — the staging table is always left
   empty between rebuilds, whatever happened above.

An **empty `coverage`** means "kept = the whole partition" — no triple is
being replaced (used when nothing succeeded for that partition this wave).

`rows_for_pair(day, extract, app_id)` (the empty-response guard's lookup,
below) deliberately stays **day**-level regardless of granularity — it asks
"did this specific day already have data", not "does the partition have data
anywhere in it"; filtering by the whole partition would let one populated day
mask a real empty-response regression on a different day of the same month.

## No row-level dedup

The destination is a plain `MergeTree` — rows are never collapsed by
content. Two CSV rows identical on every column (AppsFlyer can return this
within one pull) both get inserted and both count; this is a product
requirement, not an edge case to guard against.

## The ClickHouse 22.11 empty-source `REPLACE PARTITION` finding

**Never call `REPLACE PARTITION ... FROM staging` when staging has no part at
all for that partition.** Verified against a live 22.11 server
(`afly debug --deep` reproduces this on disposable tables): `ALTER TABLE dest
REPLACE PARTITION ID 'X' FROM staging` does **not** error when staging is
missing partition X — it silently **empties** `dest`'s partition X, as if it
never existed. This is why step 4 above is a hard branch, not an
optimization: skipping it (always calling REPLACE) would turn "this wave
fetched nothing for anyone in this partition" into "delete everyone's data in
this partition" — under `month` granularity, potentially every other day of
that month too, including triples this wave never touched.

## Crash-safety argument

- Every chunk's fetch is logged to `_afly_loads` as `running` **before** the
  pull starts, and as `success`/`failed`/`skipped` when it finishes — a crash
  mid-pull leaves a visible `running` row forever, never a silently-lost
  attempt.
- The ClickHouse write only happens **after** every job in a wave has been
  fetched (`rebuild_wave`, once per wave, not once per chunk) — so a crash
  before a wave finishes fetching touches **no** partition at all for that
  wave; the next run's watermark computation naturally re-covers the same
  window.
- `REPLACE`/`DROP PARTITION` are themselves atomic at the ClickHouse level —
  there is no "half-swapped" partition state a reader can observe.
- The empty-response guard (below) additionally protects a **successful but
  empty** pull from silently wiping real history.

Net effect: re-running the same `afly run` (whether after a crash or on
purpose) never duplicates a row, and a reader never needs `FINAL` or any
other collapse step.

## Empty-response guard (`on_empty`)

If a chunk's fetch succeeds with **zero rows** for a covered `(extract,
app_id, day)`, and that day **already has rows** for that pair in the
destination, the guard downgrades the chunk from `success` to `skipped`
(`skip_reason: "empty response guard: N existing rows kept"`) instead of
letting the rebuild wipe them — an empty AppsFlyer response is far more often
a transient hiccup than "traffic really dropped to zero." This is the default
(`on_empty: skip`); set an extract's `on_empty: replace`, or pass
`--allow-empty` for one run, to apply an empty response anyway. A pair with
**no** existing rows for that day is left as a genuine (unprotected)
`success` with `rows == 0` — there's nothing to protect yet.

## `_afly_loads` — the idempotency ledger

Append-only: `start_chunk` writes a `running` row, `finish_chunk` writes a
second, independent row with the outcome — never an in-place update.

| Column | Meaning |
|---|---|
| `run_id`, `extract`, `app_id`, `report_type` | Identity of the attempt. |
| `from_date`, `to_date`, `chunk_days`, `is_long` | The chunk pulled. |
| `status` | `running` \| `success` \| `failed` \| `skipped`. |
| `rows`, `api_calls`, `http_status` | Outcome metrics. |
| `started_at`, `finished_at`, `duration_ms` | Timing. |
| `error`, `skip_reason` | Detail for the non-success statuses. |
| `afly_version` | The afly version that wrote the row. |

**Watermark** (`LoadsRepo.watermark(extract, app_id)`) = `max(to_date)` over
`success` rows for that pair — "how far this pair has successfully loaded";
`None` if it has never succeeded. **Long-call spend today**
(`long_calls_today`) sums `api_calls` for `is_long = 1` rows started today,
**excluding `running`** — a chunk still in flight (or one that died without a
`finish_chunk`) hasn't drawn down the budget and never silently eats it
forever.

```sql
-- Failed/skipped chunks from the last run
SELECT extract, app_id, from_date, to_date, status, error, skip_reason
FROM _afly_loads
WHERE run_id = '<run_id>' AND status != 'success'
ORDER BY started_at;

-- Current watermark per (extract, app)
SELECT extract, app_id, max(to_date) AS watermark
FROM _afly_loads
WHERE status = 'success'
GROUP BY extract, app_id;
```

## `_afly_locks` — one lock per destination table

`ReplacingMergeTree(updated_at)`, always read through `FINAL` (merges aren't
synchronous, so a plain `SELECT` could see a stale row). A lock is *taken* by
inserting a `running` row and *released* by inserting a `released` row on top
— matching the append-only style of `_afly_loads`, never an in-place update.

| Column | Meaning |
|---|---|
| `lock_key` | `table:<db>.<table>` |
| `run_id`, `owner`, `status` | Who holds it (`owner` = `hostname:pid`), `running`/`released`. |
| `started_at`, `updated_at`, `timeout_seconds` | Age + staleness window. |

A `running` row older than its own `timeout_seconds`
(`afly_project.yml`'s `lock_timeout_seconds`, default 7200s) is treated as
stale and silently overridden by the next `acquire` — a crashed run's lock
self-heals without intervention, eventually. `afly unlock` clears a `running`
lock immediately, without waiting for or checking staleness.

```sql
-- Currently held locks, with computed staleness
SELECT lock_key, run_id, owner, started_at,
       dateDiff('second', started_at, now()) AS age_seconds,
       age_seconds > timeout_seconds AS stale
FROM _afly_locks FINAL
WHERE status = 'running';
```

## Why lightweight deletes aren't used

ClickHouse's `DELETE FROM ... WHERE` (mutation-based lightweight delete) marks
rows deleted asynchronously and isn't guaranteed visible to a plain `SELECT`
until the background mutation completes — on 22.11 that can lag well behind
the statement returning. A day-partition rebuild has to be *immediately* and
*atomically* consistent (the next run's watermark query, and any concurrent
reader, must never see a half-deleted day), which only `REPLACE`/`DROP
PARTITION` guarantee on this ClickHouse version.
