# Formats & the Facebook split

AppsFlyer's aggregate Pull API v5 returns CSV whose exact column set depends
on how the pull is scoped. afly detects the shape automatically and
normalizes both into the same destination row — this page explains what
you're actually getting, and why Facebook needs its own extract.

## Standard vs Facebook column shapes

A **standard** (unscoped, or non-Facebook-scoped) pull returns a `Campaign
(c)` column. A **Facebook-scoped** pull (`media_source: facebook`) instead
returns `Campaign Name` / `Campaign Id` / `Adset Name` / `Adset Id` /
`Adgroup Name` / `Adgroup Id` — the campaign/adset/adgroup breakdown Facebook
provides, which the standard shape doesn't carry at all. afly detects the
shape by the presence of the `Adset Id` header and maps both into the same
destination columns (`campaign`, `campaign_name`, `campaign_id`, `adset`,
`adset_id`, `adgroup`, `adgroup_id`) — a Facebook-shaped row's `campaign`
column is filled from `campaign_name` since it has no `Campaign (c)` header
at all.

**This is why the scaffold ships a separate `facebook` extract**
(`media_source: facebook`) instead of relying on the unfiltered `standard`
extract to surface Facebook's breakdown — AppsFlyer simply doesn't return
those columns unless the pull is scoped to Facebook.

> **Verified on a live account:** `media_source: facebook` alone is enough;
> `category: facebook` (with or without it) returns the identical report. The
> unfiltered pull carries Facebook only as aggregated `Facebook Ads` rows with
> the same total cost, so excluding `Facebook Ads` there neither loses nor
> doubles spend. If your account behaves differently, `afly debug --pull
> facebook --app <id>` prints the detected format and the real header list.

## Per-event columns

Any header shaped `"<event> (Unique users)"` / `"<event> (Event counter)"` /
`"<event> (Sales in <CUR>)"` describes one of your app's own in-app events —
the event name varies per app, so these can't be fixed columns. They land in
three `Map` columns on the destination table:

| CSV header pattern | Destination column | Example |
|---|---|---|
| `"<event> (Unique users)"` | `event_unique_users` | `event_unique_users['af_purchase']` |
| `"<event> (Event counter)"` | `event_counter` | `event_counter['af_purchase']` |
| `"<event> (Sales in <CUR>)"` | `event_sales` | `event_sales['af_purchase']` |

```sql
SELECT date, app_id, event_counter['af_purchase'] AS purchases
FROM appsflyer_geo_by_date
WHERE date = today() - 1;
```

## Currency

**AppsFlyer returns money in the app's own currency, not USD, and ignores
the `currency=USD` query param on these aggregate reports** (verified on a
live account, 2026-09-23): for a EUR app, `Total Revenue`/`Total Cost`/`ARPU`/
`Average eCPI` all arrive in EUR, and the per-event sales header itself reads
`"<event> (Sales in EUR)"` — not `"(Sales in USD)"`. afly's `EVENT_TRIPLE_RE`
recognizes `"Sales in <CUR>"` for any 3-letter code, so these no longer land
as unknown/dropped columns; the destination `currency` column (see
[Tables reference](../reference/tables.md)) records which currency every
money column of that row is in — read it, don't assume USD.

`ReportContext.currency` (an app-list-API hint, when afly has it) and the
currency detected from the CSV's own `Sales in <CUR>` headers are
cross-checked; on a disagreement the header wins (AppsFlyer's actual
behavior beats a stated account setting) and `afly debug --pull` surfaces a
warning. If a single report somehow mixes two different sales currencies
across its events, afly keeps every value in `event_sales` rather than
dropping either — again with a warning, since no single `currency` value can
describe both.

## Unknown columns

A header afly doesn't recognize (not a known dimension/metric, not an event
triple) is dropped by default, with a one-time warning naming it. Set
`keep_unknown_columns: true` on the extract to instead capture
`{header: value}` pairs into the destination `extra` column — useful while
investigating a new or renamed AppsFlyer header before deciding it needs a
real mapping.

## Value handling

Every CSV cell is a string. afly treats `""`, `N/A`, `null`, `None`, `NULL`,
and `-` as null. Numeric metrics accept thousands separators and a trailing
`.0`; percentage/currency-suffixed values have `%`/`$`/separators stripped.
A `%`-suffixed rate is stored as a whole-percent number (no `/100` scaling) —
divide in your query if you need a 0–1 fraction. The `Date` cell must be
strict ISO `YYYY-MM-DD`; anything else fails that whole chunk immediately,
since an unparseable date can't be attributed to a ClickHouse day-partition
at all.

## Checking a new pull's shape before wiring it in

```bash
afly debug --pull <extract> --app <id>
```

Non-destructive — one real Pull API call, nothing written to ClickHouse.
Prints the detected format, the real header list, any unknown headers, and a
few sample rows. Use this whenever adding an extract for a new report type
or network, before trusting what the mapping produces.
