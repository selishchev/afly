# afly

[![CI](https://github.com/selishchev/afly/actions/workflows/ci.yml/badge.svg)](https://github.com/selishchev/afly/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/afly.svg)](https://pypi.org/project/afly/)
[![Python](https://img.shields.io/pypi/pyversions/afly.svg)](https://pypi.org/project/afly/)

**Idempotent AppsFlyer aggregate reports → ClickHouse, with a dbt-style
CLI.**

`afly` pulls AppsFlyer's [aggregate Pull
API](https://support.appsflyer.com/hc/en-us/articles/207034346-Pull-API-aggregate-data) reports and
writes them into ClickHouse. It's a dbt/detectkit-style project: an
`afly_project.yml` describes the project, `profiles.yml` holds credentials,
and `extracts/*.yml` declare which reports to pull and where they land.


## Why

- **Idempotent by construction, not by convention.** Every run rebuilds
  whole ClickHouse partitions atomically (`REPLACE PARTITION`) instead of
  appending — re-running the same window, or recovering from a crash mid-run,
  never duplicates a row and never needs `FINAL` on read.
- **Quota-aware.** AppsFlyer enforces a per-minute limit and a small daily
  budget for wider date ranges. afly's scheduler interleaves apps/report
  types to stay under both automatically, and a backfill can be capped with
  `--max-calls`/`--max-minutes` and safely resumed later.
- **The Facebook split is a config concern, not a bug.** AppsFlyer only
  returns campaign/adset/adgroup breakdown columns for a Facebook-scoped
  pull — afly's scaffold ships a dedicated `facebook` extract alongside an
  unfiltered one that explicitly excludes it, so the ownership is explicit
  instead of accidentally double-counted.
- **A `--dry-run` you can actually trust.** Before pulling anything, `afly
  run --dry-run` prints the exact window/chunk plan and the AppsFlyer quota
  totals it would spend, flagging anything that would exceed budget.

## Install

```bash
pip install afly
```

Requires Python 3.10+.

## 60-second quickstart

```bash
afly init my_project && cd my_project
cp .env.example .env        # fill in AppsFlyer token + ClickHouse credentials
set -a; source .env; set +a

afly validate                       # config sanity check, no network calls
afly debug                          # probe AppsFlyer/ClickHouse connectivity
afly run --select "*" --dry-run     # print the plan, pull nothing
afly run --select "*"               # pull for real
```

```sql
SELECT date, media_source, sum(total_cost)
FROM appsflyer.appsflyer_geo_by_date
GROUP BY 1, 2
ORDER BY 1, 2;
```

See [Quickstart](docs/getting-started/quickstart.md) for the full walkthrough.

## The three-extract picture

`afly init` scaffolds a realistic split that demonstrates the ownership
convention every extra media-source extract should follow:

```yaml
# extracts/standard.yml — everything else
name: standard
report_type: geo_by_date_report
exclude_media_sources: [Facebook Ads, yandexdirect_int]   # owned below
table: appsflyer_geo_by_date

# extracts/facebook.yml — needs its own scoped pull for adset/adgroup columns
name: facebook
media_source: facebook
table: appsflyer_geo_by_date

# extracts/yandex.yml — reports arrive late, so re-pull a wider window
name: yandex
media_source: yandexdirect_int
lookback_days: 7
table: appsflyer_geo_by_date
```

All three write the same table safely, because `standard` explicitly excludes
what the other two own. See [Extracts
guide](docs/guides/extracts.md) and [Facebook
split](docs/guides/formats-and-facebook-split.md).

## AI-native onboarding

```bash
afly init-claude
```

Scaffolds `CLAUDE.md` + `.claude/rules/afly/` + four skills
(`afly-setup-project`, `afly-new-extract`, `afly-backfill`,
`afly-debug-run`) into the project, so Claude Code (or another AI assistant)
can configure extracts, size a backfill against the AppsFlyer quota, and
debug a failing run with the real reference instead of guessing. Idempotent —
safe to re-run after upgrading afly. See [Claude Code
guide](docs/guides/claude-code.md).

## Documentation

- [Installation](docs/getting-started/installation.md)
- [Quickstart](docs/getting-started/quickstart.md)
- [Configuration](docs/guides/configuration.md)
- [Extracts](docs/guides/extracts.md)
- [Formats & the Facebook split](docs/guides/formats-and-facebook-split.md)
- [Idempotency (the write path)](docs/guides/idempotency.md)
- [Quotas & scheduling](docs/guides/quotas.md)
- [Running on a schedule](docs/guides/scheduling.md)
- [Alerting](docs/guides/alerting.md)
- [Claude Code](docs/guides/claude-code.md)
- [CLI reference](docs/reference/cli.md)
- [Config reference](docs/reference/config.md)
- [Tables reference](docs/reference/tables.md)
- [Changelog](CHANGELOG.md)

## Requirements

- Python 3.10+
- ClickHouse (tested against 22.11)
- An AppsFlyer account with Pull API (API V2) access

## License

MIT License — see [LICENSE](LICENSE) for details.
