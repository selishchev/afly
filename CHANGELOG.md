# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-10-01

### Added

- `quota.transient_base_wait_seconds` (30), `quota.transient_max_wait_seconds` (600),
  `quota.retry_jitter` (0.25) — configurable, longer, jittered retry/deferral
  backoff for both `RetryPolicy` (transient errors) and `QuotaScheduler`
  (rate-limit deferrals). Jitter (`actual = nominal * (1 + retry_jitter *
  U)`) only ever lengthens a wait, so spacing never drops below AppsFlyer's
  own per-minute throttle.
- `defaults.exclude_apps` — a project-wide app-id exclusion list, UNIONed
  (not overridden) into every extract's own `exclude_apps:`.
- `alert_channels.<name>.run_url` — a link to the orchestrator's run (e.g. a
  Prefect flow-run URL), shown as a line in the alert payload. Silently
  omitted (no error, no warning) when empty or its env placeholders don't
  resolve — a laptop run has no orchestrator.

### Changed

- **Retry waits are much longer by default.** The transient-error backoff
  sequence is now 30s/60s/120s/240s (was 2s/4s/8s/16s/32s/60s-capped) before
  `max_retries` (5) is exhausted — a persistently failing chunk can now take
  ~7.5 minutes of inline sleeping, up from a few tens of seconds. Set
  `quota.transient_base_wait_seconds`/`transient_max_wait_seconds` lower to
  restore the old pacing if this doesn't suit your account.
- **The failure alert now fires on every exit-1 outcome that happens after
  the project loaded**, not only failed-chunk/abort/ClickHouse runs: an
  extract-config load error, an empty selector match, an AppsFlyer error
  resolving app lists, a held destination-table lock, and a schema mismatch
  now alert too (still exactly once per run, still never on `--dry-run` or
  an exit-0 run). A run that fails before the project/profiles config even
  loads still can't alert — there's nothing to alert *through* yet.
- **Mentions moved from the top-level Mattermost/Slack `text` into the
  attachment**, as its last line, with the run link (when resolved) on the
  line just above it; the top-level `text` is now always empty. A mention
  written without a leading `@` is normalized to exactly one. The generic
  `webhook` JSON payload gained `run_url` and `mentions` fields.
- `QuotaConfig.max_waves_in_flight`'s docstring and the shipped docs now
  correctly describe its default as `8` (code default was already `8`;
  several docs still said `2`, a stale leftover from before it was raised).

### Fixed

- `docs/reference/cli.md`'s `--json` example was still `schema_version: 1`
  (`days_rebuilt[].day`); replaced with the real `schema_version: 2` shape
  (`partition` + `days`).
- README/installation docs said "tested against 22.11"; the CI matrix (and
  `CLAUDE.md`) also run ClickHouse 26.3 — both are now named everywhere.
- The `afly init` skeleton's commented `# quota:` block was missing
  `min_gap_seconds` and the three new retry fields; it and the commented
  `defaults.exclude_apps:`/`alert_channels.*.run_url:` examples are now
  included (commented, same as the rest of that block).

## [0.1.1] - 2026-09-25

### Fixed

- AppsFlyer HTTP 404 / 408 / 416 / 425 are retried with the short transient backoff
  instead of failing the chunk at once. A live 16-month backfill got single spurious
  416/404 answers for apps that load fine on every neighbouring day; failing them
  also skipped the rest of that app/extract's history for the run.
- A failed chunk now records its HTTP status, the API calls it spent and AppsFlyer's
  response text in `_afly_loads` (was `http_status = NULL`, `api_calls = 0`, no body).
- A failed call now counts against the key's per-minute spacing, like a successful
  or rate-limited one.

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
