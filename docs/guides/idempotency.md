# Idempotency

Every `afly run` can be re-run — after a crash, on a schedule, or by
choice — without duplicating a row. This page explains the mechanism.

## The problem

ClickHouse (the version afly targets, 22.11) has neither a cheap `DELETE
FROM` nor a `ReplacingMergeTree` a plain `SELECT` can trust without `FINAL`
(expensive at scale). "Re-pull a date range without duplicating what's
already there" therefore can't be a plain append.

## The mechanism: atomic partition rebuild

afly's destination tables are partitioned by `partition_granularity`
(`afly_project.yml`'s `defaults.partition_granularity`, per-extract
overridable) — **`month`** by default (`PARTITION BY toYYYYMM(date)`), or
`day` (`PARTITION BY toYYYYMMDD(date)`). `month` is the default because a real
destination table has been measured at ~1.7MB/41k rows per 23 days — daily
partitions mean hundreds of tiny parts a year, and ClickHouse's own guidance
is coarse (monthly) partitions. Two extracts writing the **same** destination
table must agree on this (`afly validate`/`afly run` refuse to load a project
where they don't — a table has exactly one `PARTITION BY`). This is a
pre-release, no-migration project: there's no in-place way to change an
existing table's granularity (ClickHouse has no `ALTER ... MODIFY PARTITION
BY`) — recreate the table (or point the extract at a new one) if you need to
switch it.

For each **partition** a run's data touches (which, under `month`, can be
every day of a calendar month a wave fetched, not just one day), afly:

1. Copies every existing row of that partition **except** the specific
   `(day, extract, app)` triples this run is authoritative for, into a
   scratch staging table.
2. Inserts the freshly-pulled rows for those triples into the same staging
   table.
3. Atomically swaps the whole partition in: `ALTER TABLE ... REPLACE
   PARTITION` (if the staging table ended up with any rows) or `... DROP
   PARTITION` (if it ended up empty and the destination still had that
   partition at all).

The swap is all-or-nothing at the ClickHouse level — there's no partially-
applied state a reader can ever observe, and no `FINAL` is ever needed to
read the destination table correctly.

**Triples not covered by this run keep whatever they already had** —
rebuilding one extract's data for one day never touches another extract's
rows, another app's rows, or (under `month` granularity) another *day's* rows
in that same partition, even though they all share it. A month-wide partition
can legitimately be rebuilt by several consecutive waves in one run (each
wave only fetches some of the month's days) — every wave passes only the
`(day, extract, app)` triples *it itself* just fetched, so an earlier wave's
rebuild of that partition never includes a later wave's day and vice versa;
see `afly.run._rebuild.rebuild_wave`.

## Crash safety

- Every chunk pull is logged **before** it starts (a `running` row in
  `_afly_loads`) and again when it finishes — a crash mid-pull leaves a
  visible, diagnosable row, never a silent gap.
- ClickHouse writes only happen after a whole batch of chunks ("wave") has
  finished fetching — a crash before that touches **no** partition at all;
  the next run's watermark computation naturally re-covers the same window.
- Re-running the same window (on purpose, or because a previous run failed
  partway) always produces the same end state, never duplicate rows.

## No row-level dedup — by design

The destination is a plain `MergeTree`, not a `ReplacingMergeTree` — afly
never collapses rows that look alike. If AppsFlyer returns two rows identical
on every dimension within one pull (it does, occasionally), both are stored
and both count. "Idempotent" here means *re-running the same window produces
the same end state* (via the partition rebuild above), not "duplicate input
rows get deduplicated" — those are different guarantees, and only the first
one is made.

## The empty-response guard

If AppsFlyer returns **zero rows** for a day that already has data for that
`(extract, app)` pair, afly does **not** apply it by default
(`on_empty: skip`) — an empty response is far more often a transient
AppsFlyer hiccup than "this app genuinely had zero installs that day," and
applying it anyway would silently erase real history. Override per extract
with `on_empty: replace`, or for one run with `--allow-empty`.

## The bookkeeping tables

### `_afly_loads` — the idempotency ledger

One append-only row per chunk-pull attempt, recording its outcome
(`success`/`failed`/`skipped`), row count, API calls spent, and timing. This
is where the **watermark** — "how far has this (extract, app) pair
successfully loaded" — comes from, and it's the first place to look when a
run behaves unexpectedly:

```sql
-- Current watermark per (extract, app)
SELECT extract, app_id, max(to_date) AS watermark
FROM _afly_loads
WHERE status = 'success'
GROUP BY extract, app_id;

-- Anything that actually failed recently
SELECT extract, app_id, from_date, to_date, error
FROM _afly_loads
WHERE status = 'failed' AND started_at >= now() - INTERVAL 1 DAY
ORDER BY started_at DESC;
```

### `_afly_locks` — one lock per destination table

Prevents two overlapping `afly run` invocations from writing the same table
at once. A lock only outlives its run if the process crashed — it
auto-recovers after `lock_timeout_seconds` (default 2 hours), or can be
cleared immediately with `afly unlock`.

Full column reference for both tables: [Tables reference](../reference/tables.md).

## What this buys you in practice

- Scheduling `afly run` every few hours is always safe — a run that finds
  nothing new to do (window already fully loaded) is a fast no-op.
- Recovering from an outage is just running again — the watermark
  automatically reopens the gap.
- `--full-refresh` is available when you genuinely need to discard the
  watermark (e.g. after changing which media sources an extract excludes)
  and reload from `start_date`.
