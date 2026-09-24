"""Implementation of ``afly init``.

Scaffolds a new project: ``afly_project.yml``, ``profiles.yml``, three
starter extracts (standard / facebook / yandex — a realistic split that
already demonstrates the ``exclude_media_sources`` ownership convention),
plus `.env.example`, `.gitignore`, and a quickstart README.

Template strings (not Jinja) are used throughout, following detectkit's
``cli/commands/init.py`` approach — a project scaffold is static text with a
handful of substitutions, not a templating problem.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from afly.cli._output import echo_done, echo_error, echo_tree
from afly.utils.datetime_utils import today_utc

_PROJECT_YML = """name: {project_name}
version: "1.0"
default_profile: prod

paths:
  extracts: extracts

tables:
  loads: _afly_loads
  locks: _afly_locks

defaults:
  start_date: {start_date}
  lookback_days: 3          # re-pull the last N closed days on every run
  chunk_days: 2             # <= 2-day requests stay in AppsFlyer's per-minute tier (no daily quota)
  include_current_day: true
  currency: preferred        # AppsFlyer ignores this for these reports — it returns each
                              # app's own currency regardless; see the `currency` column
  on_empty: skip            # never wipe a day because AppsFlyer returned an empty report
  keep_unknown_columns: false
  partition_granularity: month  # destination PARTITION BY toYYYYMM(date) — coarse partitions,
                                 # not hundreds of tiny daily parts a year; "day" to override

# quota:                              # AppsFlyer Pull API limits (defaults shown)
#   short_call_interval_seconds: 65   # ranges <= 2 days: 1 call/min per app per report type
#                                      # (65s, not 60 — AppsFlyer's own per-minute window still
#                                      # 403s on exactly-60s spacing)
#   long_call_min_days: 3             # ranges >= 3 days count against the daily budgets below
#   account_long_calls_per_day: 120
#   app_long_calls_per_day: 24
#   reserve_long_calls: 0
#   max_retries: 5
#   max_waves_in_flight: 8            # how many plan waves may be in flight at once — lets a
#                                      # rate-limited job in one wave cool down without idling
#                                      # every other key; 1 = old strict one-wave-at-a-time order

lock_timeout_seconds: 7200

error_alerting:
  enabled: false
  channels: [ops_mattermost]
"""

_PROFILES_YML = """default_profile: prod

profiles:
  prod:
    appsflyer:
      token: "{{ env_var('APPSFLYER_TOKEN') }}"   # API V2 token (Bearer), one per account
    clickhouse:
      host: "{{ env_var('CLICKHOUSE_HOST') }}"
      # protocol: http                            # uncomment if the native port (9000) isn't reachable
      #                                            # from here — http uses clickhouse-connect over 8123
      port: 9000                                  # native protocol; omit to use the protocol's own default port
      user: "{{ env_var('CLICKHOUSE_USER') }}"
      password: "{{ env_var('CLICKHOUSE_PASSWORD') }}"
      database: appsflyer                         # destination + internal _afly_* tables

  # dev:
  #   appsflyer:
  #     token: "{{ env_var('APPSFLYER_TOKEN') }}"
  #   clickhouse:
  #     host: localhost
  #     port: 9000
  #     user: default
  #     password: ""
  #     database: afly_dev

alert_channels:
  ops_mattermost:
    type: mattermost
    webhook_url: "{{ env_var('MATTERMOST_WEBHOOK_URL') }}"
"""

_EXTRACT_STANDARD_YML = """name: standard
description: All media sources except those owned by the facebook and yandex extracts.
report_type: geo_by_date_report
category: standard
# Values of the "Media Source (pid)" column to drop. Facebook and Yandex are owned by
# their own extracts below; keeping them here too would double-count spend.
exclude_media_sources: [Facebook Ads, yandexdirect_int]
table: appsflyer_geo_by_date
tags: [daily]
"""

_EXTRACT_FACEBOOK_YML = """name: facebook
description: Facebook with campaign/adset/adgroup breakdown (AppsFlyer returns these columns only for a Facebook-scoped pull).
report_type: geo_by_date_report
# A Facebook-scoped pull is what returns Campaign Name/Id, Adset and Adgroup columns.
# `media_source: facebook` alone is enough — `category: facebook` returns the identical
# report. The Facebook format has no "Campaign (c)" column; afly fills `campaign` from
# "Campaign Name".
media_source: facebook
table: appsflyer_geo_by_date
tags: [daily]
"""

_EXTRACT_YANDEX_YML = """name: yandex
description: Yandex Direct reports arrive late — re-pull a wider window than the default.
report_type: geo_by_date_report
category: standard
media_source: yandexdirect_int
lookback_days: 7
table: appsflyer_geo_by_date
tags: [daily, late]
"""

_ENV_EXAMPLE = """APPSFLYER_TOKEN=
CLICKHOUSE_HOST=
CLICKHOUSE_USER=
CLICKHOUSE_PASSWORD=
MATTERMOST_WEBHOOK_URL=
"""

_GITIGNORE = """.env
__pycache__/
"""

_README = """# {project_name}

An afly project: pulls AppsFlyer aggregate Pull API reports and writes them
idempotently into ClickHouse.

## Quickstart

```bash
cp .env.example .env
# fill in .env, then:
set -a; source .env; set +a

afly validate                        # config sanity check, no network calls
afly debug                           # probe AppsFlyer/ClickHouse connectivity
afly run --select "*" --dry-run      # print the plan, pull nothing
afly run --select "*"                # pull everything
```

## Scheduling

Run afly on a schedule with cron, e.g. every 3 hours:

```
0 */3 * * * cd /path/to/{project_name} && afly run --select "*" --json >> afly.log
```

## Layout

- `afly_project.yml` — project-wide settings and extract defaults.
- `profiles.yml` — AppsFlyer token + ClickHouse credentials (env-interpolated).
- `extracts/` — one YAML file per report pull (`afly ls` to list them all).
"""


def _first_day_of_previous_month(today: date) -> date:
    """The default ``defaults.start_date`` a fresh project starts backfilling from.

    A full previous calendar month is enough history to validate the setup
    (retention/attribution windows settle, at least one full month closes)
    without defaulting to an expensive open-ended backfill.
    """
    first_of_this_month = today.replace(day=1)
    last_day_of_prev_month = first_of_this_month - timedelta(days=1)
    return last_day_of_prev_month.replace(day=1)


def run_init(project_name: str, target_dir: str) -> int:
    """Scaffold a new afly project under ``<target_dir>/<project_name>``.

    Returns 0 on success, 1 if the target directory already exists.
    """
    project_name_clean = Path(project_name).name
    target_path = Path(target_dir) / project_name_clean

    if target_path.exists():
        echo_error(f"'{target_path}' already exists — pick a new name or target-dir")
        return 1

    target_path.mkdir(parents=True)
    extracts_dir = target_path / "extracts"
    extracts_dir.mkdir()

    start_date = _first_day_of_previous_month(today_utc())

    (target_path / "afly_project.yml").write_text(
        _PROJECT_YML.format(project_name=project_name_clean, start_date=start_date.isoformat())
    )
    (target_path / "profiles.yml").write_text(_PROFILES_YML)
    (extracts_dir / "standard.yml").write_text(_EXTRACT_STANDARD_YML)
    (extracts_dir / "facebook.yml").write_text(_EXTRACT_FACEBOOK_YML)
    (extracts_dir / "yandex.yml").write_text(_EXTRACT_YANDEX_YML)
    (target_path / ".env.example").write_text(_ENV_EXAMPLE)
    (target_path / ".gitignore").write_text(_GITIGNORE)
    (target_path / "README.md").write_text(_README.format(project_name=project_name_clean))

    echo_tree(
        f"{project_name_clean}/",
        [
            "afly_project.yml",
            "profiles.yml",
            "extracts/standard.yml",
            "extracts/facebook.yml",
            "extracts/yandex.yml",
            ".env.example",
            ".gitignore",
            "README.md",
        ],
    )
    echo_done(f"Created project '{project_name_clean}' in {target_path}.")
    return 0
