# afly — extracts (`extracts/*.yml`)

One file = one AppsFlyer Pull API report configuration + one destination
table. Discovered recursively under `paths.extracts` (default `extracts/`),
any `*.yml`/`*.yaml` not inside a hidden (dot-prefixed) directory. `name:`
must be unique across the whole project — it is the selector key and the
idempotency/lock key (`_afly_loads.extract`, `_afly_locks.lock_key` via the
table), so a collision is a hard load error.

## Fields

```yaml
name: standard                    # required, pattern ^[a-z0-9_]+$, unique project-wide
description: "..."                # optional, free text
enabled: true                     # default true — disabled extracts are skipped (not an error)

report_type: geo_by_date_report   # required — see "Supported report types" below
category: standard                # "standard" | "facebook" | "organic", default "standard"
media_source: facebook            # optional — scope the pull to one AppsFlyer media source
exclude_media_sources: []         # optional — drop these values of the CSV's Media Source column
reattr: false                     # default false — pull the retargeting/reattribution dataset
attribution_touch_type: null      # "click" | "impression" | null (impression = view-through)
timezone: null                    # optional override of defaults.timezone
currency: null                    # "preferred" | "USD" | null (falls back to defaults.currency) —
                                   # AppsFlyer ignores this for these reports regardless of the value;
                                   # see the destination `currency` column / formats.md

apps: null                        # optional explicit list of AppsFlyer app ids
exclude_apps: []                  # subtracted from apps: (or from the account's full app list)
platforms: null                   # optional platform filter (e.g. ["ios"]) when apps: is unset

start_date: 2026-01-01            # optional here IF defaults.start_date is set project-wide
lookback_days: null               # optional override of defaults.lookback_days
include_current_day: null         # optional override of defaults.include_current_day
chunk_days: null                  # optional override of defaults.chunk_days (1..90)
on_empty: null                    # optional override of defaults.on_empty
keep_unknown_columns: null        # optional override of defaults.keep_unknown_columns
partition_granularity: null       # "month" | "day" | null (falls back to defaults.partition_granularity) —
                                   # extracts sharing one table: must resolve to the same value

table: appsflyer_geo_by_date      # required — "table" (profile database) or "db.table"
extra_params: {}                  # optional extra AppsFlyer query params (never "from"/"to")
tags: [daily]                     # optional, used by --select tag:<t>
```

### Supported report types

v1 supports only the **by-date** report family — `geo_by_date_report`,
`partners_by_date_report`, `daily_report`. These carry a `Date` column, which
the idempotent day-partition write relies on. `partners_report` and
`geo_report` are **rejected at load time** (not silently mis-loaded): they are
totals-over-the-requested-range reports with no `Date` column at all.

## Defaults inheritance (`with_defaults`)

Every extract merges through `afly_project.yml`'s `defaults:` block for the
fields marked optional above (`start_date`, `lookback_days`, `chunk_days`,
`include_current_day`, `timezone`, `currency`, `on_empty`,
`keep_unknown_columns`) — an extract only states what makes it *different*
from the project norm. If `start_date` is still unset after the merge (unset
on both the extract and `defaults`), that extract fails to load with a named
error — unlike the other fields, there is no safe built-in fallback for
"which day does history start on".

## Validation rules (fail the load)

- `report_type` in `{partners_report, geo_report}` — unsupported (see above).
- `category: organic` together with `media_source:` set — organic traffic has
  no media source by definition.
- `media_source:` together with a non-empty `exclude_media_sources:` — these
  are mutually exclusive ways of scoping the pull; pick one.
- `extra_params` containing `from`/`to` — the pull window is controlled by
  afly (watermark + `--from`/`--to`), never by a raw extra param.
- `table:` must parse as `table` or `db.table` (exactly zero or one dot).
- `name:` colliding with another extract's `name:` anywhere in the project.
- Two extracts writing the same `table:` that resolve (after defaults) to
  different `partition_granularity` — a destination table has exactly one
  `PARTITION BY`; see `idempotency.md`.

## Validation warnings (`afly validate`, non-fatal)

- **`chunk_days >= quota.long_call_min_days`** — flags that this extract's
  chunks count against the daily long-call budget; not wrong, just worth
  knowing (see `quotas.md`).
- **Possible double-counted media source** — two extracts write the **same**
  `table:`, one is unfiltered (`media_source:` unset) and the other is scoped
  to a specific `media_source`, and the unfiltered one's
  `exclude_media_sources:` does not (even loosely — the check normalizes
  case/punctuation and matches by prefix, e.g. `facebook` vs `Facebook Ads`)
  cover that media source. This is exactly the ownership mistake the scaffold
  extracts are built to avoid — see below.

## The three scaffold extracts (`afly init`)

```yaml
# extracts/standard.yml — everything NOT owned by another extract
name: standard
report_type: geo_by_date_report
category: standard
exclude_media_sources: [Facebook Ads, yandexdirect_int]   # owned by the extracts below
table: appsflyer_geo_by_date
tags: [daily]
```

```yaml
# extracts/facebook.yml — Facebook needs its own scoped pull for campaign/adset/adgroup columns
name: facebook
report_type: geo_by_date_report
media_source: facebook        # enough on its own; category: facebook returns the same report
table: appsflyer_geo_by_date   # same destination table as standard — ownership makes this safe
tags: [daily]
```

```yaml
# extracts/yandex.yml — Yandex Direct reports arrive late, so re-pull a wider window
name: yandex
report_type: geo_by_date_report
category: standard
media_source: yandexdirect_int
lookback_days: 7               # overrides defaults.lookback_days (usually 3)
table: appsflyer_geo_by_date
tags: [daily, late]
```

All three write the **same** destination table — this is intentional (one
unified table, filtered pulls own their own slice of it) and only works
because `standard`'s `exclude_media_sources` names exactly what `facebook` and
`yandex` pull. Adding a fourth scoped extract without adding its media source
to `standard`'s exclusion list is the double-count bug `afly validate` warns
about.
