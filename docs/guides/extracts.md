# Extracts

An extract (`extracts/<name>.yml`) is one AppsFlyer report pull plus the
ClickHouse table it writes to. `name:` is both the `--select` key and the
idempotency key, so it must be unique across the whole project.

## A minimal extract

```yaml
name: standard
report_type: geo_by_date_report
table: appsflyer_geo_by_date
```

Everything else — `start_date`, `lookback_days`, `chunk_days`,
`include_current_day`, `currency`, `on_empty`, `keep_unknown_columns` — falls
back to `afly_project.yml`'s `defaults:` block. Override only what this
extract needs to differ. Full field list:
[Config reference](../reference/config.md#extract-extractsnameyml).

## Supported report types

afly loads only the **by-date** report family — `geo_by_date_report`,
`partners_by_date_report`, `daily_report`. These carry a `Date` column, which
the idempotent write path (one ClickHouse partition per day) depends on.
`partners_report`/`geo_report` are rejected at config-load time: they total
over the requested range with no `Date` column, and can't be loaded the same
way.

## Scoping a pull

- **`media_source`** restricts the pull to one AppsFlyer network
  (`facebook`, `yandexdirect_int`, …).
- **`category`** — `standard` (default), `facebook`, or `organic`. Some
  networks return different columns depending on `category` — see
  [Formats & the Facebook split](formats-and-facebook-split.md).
- **`reattr: true`** pulls the retargeting/reattribution dataset instead of
  installs — give it its own extract, don't mix the two in one pull.
- **`attribution_touch_type: impression`** switches to view-through
  attribution (the default, `click`, needs no setting).

## The ownership convention (avoiding double-counted spend)

Several extracts can write to the **same table** — this is how the scaffold
works: `standard`, `facebook`, and `yandex` all write
`appsflyer_geo_by_date`. It's safe *only* because the unfiltered extract
excludes what the scoped ones own:

```yaml
# standard.yml — everything NOT pulled by a scoped sibling
exclude_media_sources: [Facebook Ads, yandexdirect_int]

# facebook.yml — its own scoped pull
media_source: facebook

# yandex.yml — its own scoped pull, with a wider lookback for late data
media_source: yandexdirect_int
lookback_days: 7
```

Add a new scoped extract (say, TikTok) writing to the same table, and you
must add its `media_source` value to the unfiltered extract's
`exclude_media_sources` too — otherwise both extracts pull overlapping rows
into the same table, and every downstream sum double-counts that network.

`afly validate` catches a missed case with a warning (matching is
case/punctuation-tolerant: `facebook` vs `Facebook Ads` still counts as
covered) — but it's a warning, not a hard error, so don't rely on it alone.
The [`afly-new-extract` Claude skill](claude-code.md) walks this check as an
explicit step.

## Apps

```yaml
apps: ["123456789", "987654321"]   # explicit list
# or leave unset for the whole account, optionally narrowed:
platforms: [ios]
exclude_apps: ["111111111"]
```

An explicit `apps:` list is resolved with no AppsFlyer call; leaving it unset
triggers one (cached) call to the management API per run, filtered by
`platforms`/`exclude_apps`. Cross-check ids against `afly apps`.

`exclude_apps:` here is **UNIONED** with the project's own
`defaults.exclude_apps:` (see [Configuration](configuration.md)), not
overridden by it — a project-wide exclusion (e.g. a decommissioned test app)
applies to every extract even if this one also names its own exclusions. An
explicit `afly run --apps` can't bring back an app either list excludes.

## Validation

```bash
afly validate --select "<extract or selector>"
```

Checks every field's shape (report type, mutual exclusions like
`media_source` + `exclude_media_sources`, a parseable `table:`) and surfaces
the non-fatal warnings above. Runs offline — never touches AppsFlyer or
ClickHouse.
