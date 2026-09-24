# afly Documentation

**afly** — idempotent AppsFlyer aggregate Pull API → ClickHouse extraction,
with a dbt-style CLI.

## Quick links

- **[Installation](getting-started/installation.md)** — install afly
- **[Quickstart](getting-started/quickstart.md)** — first pull in 5 minutes
- **[CLI reference](reference/cli.md)** — every command and option

## Getting started

- **[Installation](getting-started/installation.md)**
- **[Quickstart](getting-started/quickstart.md)**

## Guides

- **[Configuration](guides/configuration.md)** — `afly_project.yml` and
  `profiles.yml`, env interpolation, profiles
- **[Extracts](guides/extracts.md)** — extract fields, defaults inheritance,
  the ownership convention for shared destination tables
- **[Formats & the Facebook split](guides/formats-and-facebook-split.md)** —
  the AppsFlyer CSV shapes, event-triple columns, why Facebook needs its own
  scoped pull
- **[Idempotency](guides/idempotency.md)** — the partition rebuild write
  path (configurable `month`/`day` granularity), crash-safety, the internal
  bookkeeping tables
- **[Quotas & scheduling](guides/quotas.md)** — AppsFlyer's rate-limit
  tiers, the afly scheduler, sizing a backfill
- **[Running on a schedule](guides/scheduling.md)** — cron and a Prefect
  flow example
- **[Alerting](guides/alerting.md)** — run-failure notifications
- **[Claude Code](guides/claude-code.md)** — AI-assisted project setup

## Reference

- **[CLI reference](reference/cli.md)** — every command and option
- **[Config reference](reference/config.md)** — every `afly_project.yml` /
  `profiles.yml` / extract field, with defaults
- **[Tables reference](reference/tables.md)** — destination table columns,
  `_afly_loads`, `_afly_locks`

## Contributing to afly itself

Working on afly's own source (not a project that uses it)? See the
repo-level [`CLAUDE.md`](../CLAUDE.md) and
[`.claude/rules/contributing.md`](../.claude/rules/contributing.md).
