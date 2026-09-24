---
name: afly-setup-project
description: >-
  Configure a fresh afly project's credentials (.env) and verify it end to
  end: validate config, check live connectivity, dry-run the plan, then run
  for real. Use right after `afly init`, for first-time setup, or when a run
  fails with an unresolved environment variable, "file not found", or a
  connection error.
---

# Set up an afly project

Turn a freshly-scaffolded project (`afly init <name>`) into one that pulls
data, verifying at each step. Don't invent tokens/hosts/webhook URLs — gather
them from the user. Field detail: `.claude/rules/afly/project.md`.

1. **Locate the project.** A root has `afly_project.yml` + `profiles.yml`
   (find it, or the nearest ancestor). Missing → `afly init <name>` first.

2. **Credentials into `.env`.** `cp .env.example .env`, then fill in what
   `profiles.yml` references: `APPSFLYER_TOKEN` (API V2 Bearer token),
   `CLICKHOUSE_HOST`/`_USER`/`_PASSWORD` (**native protocol** — port 9000 by
   default, not the HTTP port 8123), `MATTERMOST_WEBHOOK_URL` only if
   `error_alerting.enabled: true`. Never paste a credential into YAML
   directly. Load it: `set -a; source .env; set +a`.

3. **Config check (no network).** `afly validate` — must report 0 errors. A
   warning about an unset env var means step 2 isn't done; warnings about
   `exclude_media_sources`/`chunk_days` are about the extracts, fine to
   defer.

4. **Live connectivity.** `afly debug --deep` (the `--deep` flag also proves
   the ClickHouse `REPLACE`/`DROP PARTITION` mechanics on disposable
   tables — worth it once per new target). AppsFlyer unreachable/401 → bad
   token. ClickHouse connect failure → wrong host/port or bad credentials.
   `grants` found nothing → the user may lack ALTER/INSERT/CREATE TABLE.

5. **See the plan first.** `afly run --select "*" --dry-run` — check the
   estimated wall time and quota totals aren't `EXCEEDS`
   (`.claude/rules/afly/quotas.md`). For a genuinely wide backfill, hand off
   to the **`afly-backfill`** skill instead of running it all at once.

6. **First real run.** `afly run --select "*"`, then verify:

   ```sql
   SELECT date, media_source, sum(total_cost)
   FROM <database>.<table>
   GROUP BY 1, 2 ORDER BY 1, 2 LIMIT 20;
   ```

7. **Claude context (optional).** `afly init-claude` — idempotent, safe to
   re-run any time; refreshes to match the installed afly version.

## Final checklist

- [ ] `.env` has every credential `profiles.yml` needs, sourced into the shell.
- [ ] `afly validate` reports 0 errors; `afly debug --deep` passes.
- [ ] `--dry-run` shows a sane window, no `EXCEEDS` line.
- [ ] A real run produced rows, confirmed by `SELECT`.

Then: **`afly-new-extract`** for a new report, or **`afly-backfill`** for a
wide historical load.
