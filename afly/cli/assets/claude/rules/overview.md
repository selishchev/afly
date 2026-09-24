# afly — overview

afly idempotently pulls AppsFlyer aggregate [Pull
API](https://support.appsflyer.com/hc/en-us/articles/207034366) reports and
writes them into ClickHouse. It is a dbt/detectkit-style project: one
`afly_project.yml`, one `profiles.yml` for credentials, and an `extracts/`
directory of YAML files, each declaring one AppsFlyer report pull and where it
lands.

## Pipeline

`afly run` does, per invocation:

1. **Select** — `--select`/`--exclude` (dbt-style selector grammar) resolve
   which `extracts/*.yml` to run.
2. **Resolve apps** — each extract's `apps:` (explicit list) or the whole
   AppsFlyer account's app list, filtered by `platforms:`/`exclude_apps:`.
3. **Plan** — for every (extract, app) **pair**, compute a pull **window**
   from the watermark (the last successful load) and `lookback_days`, then
   split it into epoch-aligned date **chunks** of `chunk_days`. Chunks that
   share the same epoch-aligned index, across every extract, form a **wave**.
4. **Schedule** — a quota-aware scheduler hands out jobs from up to
   `quota.max_waves_in_flight` waves at once (oldest first), round-robin
   across `(app_id, report_type)` keys, respecting AppsFlyer's per-minute
   and per-day rate limits (`quotas.md`).
5. **Fetch + parse** — each job pulls one CSV report and parses it into rows
   matching the destination schema (`formats.md`).
6. **Rebuild** — once every job in a wave has run, afly rewrites each touched
   ClickHouse partition atomically (grouped by `partition_granularity`,
   `month` by default): old rows for the (day, extract, app) triples this
   wave is authoritative for are dropped, fresh rows are inserted
   (`idempotency.md`). Rebuilds always happen strictly in wave order, even
   though dispatch across the lookahead window doesn't.
7. **Record + alert** — every chunk attempt is logged to `_afly_loads`; a
   failed/aborted run (never a quota/policy skip) triggers a once-per-run
   alert if `error_alerting.enabled`.

## Destination tables

- **One table per extract** (usually shared by several extracts, e.g. the
  `standard`/`facebook`/`yandex` scaffold all write `appsflyer_geo_by_date`) —
  the normalized row shape afly writes is `afly.schema.DESTINATION_COLUMNS`:
  dimension columns (`app_id`, `date`, `media_source`, `campaign`, `country`,
  `currency`, …), metric columns (`impressions`, `installs`, `total_cost`,
  …), three `Map` columns for per-in-app-event triples
  (`event_unique_users`, `event_counter`, `event_sales`), and bookkeeping
  (`_extract`, `_run_id`, `_loaded_at`). `currency` records which currency
  every money column of that row is in — AppsFlyer returns each app's own
  currency for these reports, not a fixed one; see `formats.md`. See
  `docs/reference/tables.md` for the full column list.
- **`_afly_loads`** — append-only ledger: one row per chunk-pull attempt
  (`running` at start, `success`/`failed`/`skipped` at finish). This is both
  the idempotency watermark source and the quota-spend ledger.
- **`_afly_locks`** — one row per destination table currently being written,
  so two overlapping `afly run` invocations can't corrupt the same table.

Both bookkeeping tables live in the profile's `internal_database` (defaults to
the same database as the destination tables).

## Glossary

| Term | Meaning |
|---|---|
| **extract** | One `extracts/*.yml` — one AppsFlyer report configuration + destination table. |
| **pair** | One `(extract, app_id)` — the idempotency/coverage unit. Its watermark and window are computed independently of every other pair. |
| **chunk** | One epoch-aligned date range (≤ `chunk_days` long) — one AppsFlyer API call for one pair. |
| **wave** | Every chunk across every extract/app sharing the same epoch-aligned chunk index. Rebuilds execute strictly in wave order so a day's ClickHouse partition is rebuilt once it has every extract's contribution, not once per extract; *dispatch* may draw from up to `quota.max_waves_in_flight` waves at once (`quotas.md`). |
| **watermark** | The latest `to_date` of a `success` row in `_afly_loads` for a given pair — "how far this pair has successfully loaded". |
| **coverage** | The set of `(extract, app_id)` pairs a wave's rebuild is authoritative for on a given day — their old rows are dropped and replaced regardless of whether the fresh pull returned anything (see `idempotency.md`). |
| **long call** | A chunk of ≥ `quota.long_call_min_days` days — counts against AppsFlyer's daily budget. Everything shorter is a "short call" (per-minute tier only). |

## Where the pieces live (for a contributor, not a user)

This section is about the packaged reference for an afly *project* — if you're
instead working on afly's own source, see the repo-level `CLAUDE.md` and
`.claude/rules/architecture.md` in the afly repository itself, not here.
