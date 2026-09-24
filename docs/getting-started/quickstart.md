# Quickstart

Five minutes from a fresh install to real rows in ClickHouse.

## 1. Install

```bash
pip install afly
```

## 2. Scaffold a project

```bash
afly init my_project
cd my_project
```

This creates `afly_project.yml`, `profiles.yml`, three starter extracts under
`extracts/` (`standard`, `facebook`, `yandex` — see [the Extracts
guide](../guides/extracts.md) for what each one does), `.env.example`, and a
`.gitignore` that excludes `.env`.

## 3. Credentials

```bash
cp .env.example .env
```

Edit `.env` with your real values:

```dotenv
APPSFLYER_TOKEN=your-appsflyer-api-v2-token
CLICKHOUSE_HOST=your-clickhouse-host
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=your-password
MATTERMOST_WEBHOOK_URL=          # optional, only needed if you enable error_alerting
```

`profiles.yml` references these via `{{ env_var('...') }}` — nothing sensitive
ever needs to live in a committed file. Load them into your shell before
every `afly` command below:

```bash
set -a; source .env; set +a
```

## 4. Validate the config (no network calls)

```bash
afly validate
```

Should report `0 errors`. A warning about an unset environment variable means
`.env` wasn't sourced, or a value is still blank.

## 5. Check live connectivity

```bash
afly debug
```

Confirms the AppsFlyer token works and ClickHouse is reachable, without
writing anything. Add `--deep` to also verify the exact `REPLACE`/`DROP
PARTITION` mechanics afly's write path relies on, on disposable tables.

## 6. See the plan before pulling anything

```bash
afly run --select "*" --dry-run
```

Prints, per extract: the resolved apps, the pull window, the chunk/long-call
counts, and the AppsFlyer quota totals this run would spend (flagged
`EXCEEDS` if over budget) — no HTTP calls to AppsFlyer, no ClickHouse writes.

## 7. Pull for real

```bash
afly run --select "*"
```

## 8. Verify

```sql
SELECT date, media_source, sum(total_cost)
FROM appsflyer.appsflyer_geo_by_date
GROUP BY 1, 2
ORDER BY 1, 2;
```

(`appsflyer` is the scaffold's default database name — adjust to whatever
`profiles.yml`'s `clickhouse.database` is set to.)

## Next steps

- Loading a lot of history? See [Quotas &
  scheduling](../guides/quotas.md) before widening the date range — a
  backfill sized against AppsFlyer's rate limits finishes faster than one
  that isn't.
- Want it running automatically? See [Running on a
  schedule](../guides/scheduling.md).
- Adding another media source or report? See [Extracts](../guides/extracts.md).
- Want an AI assistant to help with the above? Run `afly init-claude` — see
  [Claude Code](../guides/claude-code.md).
