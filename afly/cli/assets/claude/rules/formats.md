# afly — AppsFlyer CSV formats

AppsFlyer's aggregate Pull API v5 returns a CSV whose exact column set
depends on the request scope. afly parses it into rows matching
`afly.schema.DESTINATION_COLUMNS` (`docs/reference/tables.md`) via
`afly.csvmap`.

## `category` / `media_source` / `reattr` / `attribution_touch_type`

These four extract fields become AppsFlyer query params (never edited by
hand — `PullRequestSpec.from_extract` / `build_pull_request`):

| Extract field | Query param | When sent |
|---|---|---|
| `category` | `category` | Only when **not** `"standard"` (the API default) — `"facebook"` or `"organic"`. |
| `media_source` | `media_source` | When set — scopes the whole pull to one AppsFlyer media source. |
| `reattr` | `reattr=true` | Only when `true` — pulls the **retargeting/reattribution** dataset instead of the normal (install) one. |
| `attribution_touch_type` | `attribution_touch_type=impression` | Only for `"impression"` (view-through attribution). `"click"` is the API default and sends no param. |

## Standard vs Facebook column shapes

`afly.csvmap.headers.detect_format()` looks for the `Adset Id` header — its
presence is the signal that this response is Facebook-shaped:

| CSV header | Destination column | Shape |
|---|---|---|
| `Date` | `date` | both |
| `Country` | `country` | both |
| `Agency/PMD (af_prt)` | `agency` | both |
| `Media Source (pid)` | `media_source` | both |
| `Campaign (c)` | `campaign` | standard |
| `Campaign Name` | `campaign_name` | Facebook |
| `Campaign Id` | `campaign_id` | Facebook |
| `Adset Name` | `adset` | Facebook |
| `Adset Id` | `adset_id` | Facebook — the format marker |
| `Adgroup Name` | `adgroup` | Facebook |
| `Adgroup Id` | `adgroup_id` | Facebook |
| `Impressions` / `Clicks` / `CTR` / `Installs` / `Conversion Rate` / `Sessions` / `Loyal Users` / `Loyal Users/Installs` | matching lowercase column | both |
| `Total Revenue` / `Total Cost` / `ROI` / `ARPU` / `Average eCPI` | matching lowercase column | both |

A Facebook-shaped response has no `Campaign (c)` header at all — the parser
fills the destination `campaign` column from `campaign_name` in that case
(`has_campaign_c` false, `has_campaign_name` true), so downstream queries can
always read `campaign` regardless of which shape produced the row. Header
matching is exact first, then a case-insensitive fallback (flagged, not
dropped) for header casing drift observed across AppsFlyer report-format
versions.

**Verified against a live account:** `media_source: facebook`, `category:
facebook`, or both return the *identical* Facebook-shaped report (same rows,
same columns, same cost), so the scaffold sets only `media_source: facebook`.
The unfiltered pull returns Facebook as a couple of aggregated `Facebook Ads`
rows with no adset/adgroup columns but the **same total cost** — which is why
the unfiltered extract excludes `Facebook Ads` rather than losing or doubling
spend. `afly debug --pull facebook --app <id>` prints the detected `format`
and `headers` if an account ever behaves differently.

## Event triples → `Map` columns

Any header of the shape `"<event> (Unique users)"` / `"<event> (Event
counter)"` / `"<event> (Sales in <CUR>)"` is a **per-in-app-event** triple —
the event name varies per report (it's whatever in-app events that app
tracks), so these can't be fixed destination columns. Each kind maps to one
`Map` column, keyed by the event name:

| Triple kind | Destination column | Value type |
|---|---|---|
| `(Unique users)` | `event_unique_users` | `Map(String, UInt64)` |
| `(Event counter)` | `event_counter` | `Map(String, UInt64)` |
| `(Sales in <CUR>)` | `event_sales` | `Map(String, Float64)` |

E.g. `"af_purchase (Event counter)"` → `event_counter['af_purchase'] = <n>`.

**`<CUR>` is not always `USD`.** AppsFlyer reports Sales (and every other
money column — `Total Revenue`/`Total Cost`/`ARPU`/`Average eCPI`) in the
app's *own* currency and ignores the API's `currency=USD` param for these
aggregate reports — a EUR app's header literally reads `"(Sales in EUR)"`.
The destination `currency` column (right after `country`) records which
currency every money column of that row is in; `EVENT_TRIPLE_RE` accepts any
3-letter code, so a non-USD app's Sales header is recognized, not dropped as
unknown.

## Unknown headers

A header that matches neither a known column nor the event-triple pattern is
"unknown". By default its value is **dropped** (and the header name surfaces
once as a warning — `afly debug --pull` and `afly run`'s stderr both print
it). Set the extract's `keep_unknown_columns: true` to instead stash
`{header: value}` pairs into the destination `extra` (`Map(String, String)`)
column — useful while investigating a new/renamed AppsFlyer header before
deciding whether it needs a real mapping.

## Null tokens and value coercion

Every cell arrives as a string. `afly.csvmap.values` recognizes
`""`, `"N/A"`, `"null"`, `"None"`, `"NULL"`, `"-"` as null (→ `""` for a
dimension, `None` for a metric) regardless of column. Metric coercion:

- **uint columns** (`impressions`, `clicks`, `installs`, `sessions`,
  `loyal_users`): thousands separators and a trailing `.0` are accepted
  (`"12,000"`, `"12.0"`); a negative or unparseable value becomes `None`.
- **float columns** (`ctr`, `conversion_rate`, `loyal_users_rate`,
  `total_revenue`, `total_cost`, `roi`, `arpu`, `average_ecpi`): `%`/`$`/
  thousands separators are stripped before parsing; a `%`-suffixed rate stays
  in whole-percent units (no `/100` scaling) — that's the caller's choice to
  make downstream, not the parser's.
- **`date`**: strict ISO `YYYY-MM-DD` — anything else raises immediately,
  naming the offending row number, since a report with an unparseable date
  can't be attributed to any ClickHouse partition at all. Every other
  cell-level problem degrades to a null instead of failing the whole report —
  one bad metric shouldn't sink an otherwise-good day's data.

Rows outside the requested `--from`/`--to` window, or whose `media_source`
matches the extract's `exclude_media_sources`, are dropped before reaching the
destination — both counts are reported by `afly debug --pull`
(`dropped_out_of_range`, `dropped_excluded`).
