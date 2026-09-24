---
name: afly-new-extract
description: >-
  Scaffold a new afly extract (extracts/<name>.yml) — an AppsFlyer report
  pull plus its destination table. Use when the user wants to add a new
  AppsFlyer report, pull a new media source, or add a Facebook/Yandex-style
  scoped pull alongside an existing one. Produces a file that passes `afly
  validate` with no double-count warning.
---

# Create a new afly extract

Scaffold one `extracts/<name>.yml` that **validates cleanly**, including the
double-count check against siblings. Don't invent report types, media
source names, or table names — gather them. Field detail:
`.claude/rules/afly/extracts.md` (CSV-shape consequences:
`.claude/rules/afly/formats.md`).

0. **Confirm you're in an afly project.** Find `afly_project.yml` (or the
   nearest ancestor); missing → `afly init <name>` first. Note
   `paths.extracts` and what `defaults:` already covers.

1. **Name and report type.** `name`: lowercase snake_case, unique
   project-wide (grep `extracts/**/*.yml` for a clash — it's both the
   selector and the idempotency key). `report_type`: almost always
   `geo_by_date_report`; never `partners_report`/`geo_report` (no `Date`
   column, rejected at load time).

2. **Scope: what does this extract own?** Everything not covered elsewhere
   → `category: standard`, no `media_source`, and an
   `exclude_media_sources:` naming every sibling that writes the same
   `table`. One specific network (own breakdown columns, own
   `lookback_days`) → `media_source: <value>` (for Facebook,
   `media_source: facebook` alone yields the adset/adgroup format;
   `category: facebook` is equivalent, not additionally required). Retargeting → its own extract with `reattr: true`.
   View-through only → `attribution_touch_type: impression`.

3. **Check for double-counting.** Sharing a `table:` with an existing
   extract? If the new one is **scoped**, add its `media_source` to the
   unfiltered sibling's `exclude_media_sources:`. If the new one is itself
   **unfiltered**, list every sibling's `media_source` there. `afly
   validate` catches a missed case (casing/punctuation-tolerant) — fix it
   now rather than relying on the warning.

4. **Apps, window, destination.** `apps:`/`platforms:`/`exclude_apps:` as
   needed. Leave `start_date`/`lookback_days`/`chunk_days`/
   `include_current_day`/`on_empty`/`keep_unknown_columns` unset unless this
   extract genuinely differs from the project `defaults:`. `table:` — reuse
   a name to add this extract's slice to it, or pick a new one. `tags:` —
   match the project's existing vocabulary (`afly ls` to check).

5. **Write the file**, and update the sibling from step 3 if applicable:

   ```yaml
   # extracts/tiktok.yml
   name: tiktok
   report_type: geo_by_date_report
   media_source: tiktokglobal_int
   table: appsflyer_geo_by_date
   tags: [daily]
   ```
   ```yaml
   # extracts/standard.yml
   exclude_media_sources: [Facebook Ads, yandexdirect_int, tiktokglobal_int]
   ```

6. **Validate.** `afly validate --select "<name>,<edited sibling>"` — 0
   errors, **no double-count warning**. Then confirm the real CSV shape
   (non-destructive): `afly debug --pull <name> --app <real_app_id>` — check
   `headers`/`unknown_headers` match what you expected; adjust
   `category`/`media_source` if not.

7. **Report** the created/edited files and the commands above. Offer `afly
   run --select "<name>" --dry-run`, or hand off to **`afly-backfill`** for
   a wide historical load.
