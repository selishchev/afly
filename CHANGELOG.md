# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-09-24

First public release.

### Added

- `afly init <name>` — scaffold a project: `afly_project.yml`, `profiles.yml`
  (credentials via `{{ env_var('…') }}` / `${…}`), `.env.example`, and three
  starter extracts (`standard`, `facebook`, `yandex`) demonstrating the
  `exclude_media_sources` ownership convention.
- `afly run` — idempotent AppsFlyer aggregate Pull API → ClickHouse extraction:
  - watermark-based windows with per-extract `lookback_days`, epoch-aligned
    `chunk_days` chunks, `--from/--to/--full-refresh`;
  - partition rebuild via staging + `REPLACE PARTITION` (or `DROP PARTITION`
    when nothing remains): no duplicates, no `FINAL` for readers, rows that
    vanish from AppsFlyer vanish from the table, no row-level dedup (rows
    identical on every dimension within one pull are all kept);
  - `partition_granularity: month | day` (default `month`);
  - quota-aware scheduler: per-key spacing, daily long-call budgets,
    rate-limited jobs deferred without blocking other apps, lookahead across
    waves (`max_waves_in_flight`), `--max-calls/--max-minutes`;
  - empty-response guard, per-table locks, `--dry-run`, `--json` summary.
- One destination table for all apps of an account, snake_case columns with
  `app_id`, Facebook campaign/adset/adgroup detail, in-app events as
  `Map` columns, and a `currency` column (AppsFlyer reports money in each
  app's own currency).
- ClickHouse over the native protocol or HTTP (`protocol: native | http`),
  tested on ClickHouse 22.11 and 26.3; optional separate `internal_database`
  and `staging_database`.
- `afly ls`, `afly validate`, `afly apps`, `afly debug` (connectivity, grants,
  `--deep` partition-swap drill, `--pull` sample pull), `afly unlock`.
- `afly init-claude` — Claude Code context (`CLAUDE.md` block,
  `.claude/rules/afly/`, four skills), idempotent and refreshable.
- Failure alerting to Mattermost/Slack/webhook, once per run, never on quota
  or policy skips.
- Documentation in `docs/`: getting started, configuration, extracts, formats
  and the Facebook split, idempotency, quotas, scheduling, alerting.
