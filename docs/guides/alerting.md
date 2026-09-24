# Alerting

afly can notify a webhook channel when a run **fails** — a run-level failure
alert, not a data-quality one. It fires **at most once per run**, and only
for a real failure or abort:

- at least one chunk ended `failed`, or
- the run aborted early (an authentication failure, or a ClickHouse
  partition-rebuild failure).

It never fires for a quota/policy skip (a rate limit, `--max-calls`, the
empty-response guard, a disabled extract) — those are expected outcomes, not
failures. It also never fires for a config-load error before a run even
starts (nothing to alert *from* yet) — that surfaces as a non-zero exit code
for your scheduler to catch instead (see [Scheduling](scheduling.md)).

## Enable it

```yaml
# afly_project.yml
error_alerting:
  enabled: true
  channels: [ops_mattermost]
  mentions: ["@oncall"]           # optional, passed through to the channel
```

```yaml
# profiles.yml
alert_channels:
  ops_mattermost:
    type: mattermost
    webhook_url: "{{ env_var('MATTERMOST_WEBHOOK_URL') }}"
    channel: alerts                # optional
    username: afly                 # optional, default "afly"
```

`error_alerting.channels` names must exist in `profiles.yml`'s
`alert_channels` — an unknown name is skipped with a warning, not a hard
failure (so a typo doesn't crash an otherwise-successful-looking run).

## Channel types

| `type` | Delivery |
|---|---|
| `mattermost` | Incoming webhook, native "attachments" payload. |
| `slack` | Same "attachments" shape — Slack's incoming webhooks accept it. |
| `webhook` | Generic — `{"title", "text", "project", "run_id"}` JSON body, for anything else (PagerDuty relay, a custom endpoint, …). |

Every channel type shares `webhook_url` (required), `channel`, `username`
(default `afly`), `icon_emoji`, and `timeout` (default 10s). Full field
list: [Config reference](../reference/config.md).

## What the alert contains

A one-line summary (`afly run failed: <project>/<profile>`), the abort
reason if the run aborted, and up to 10 failed chunks
(`extract app_id from..to: error`), with a "... and N more" tail if there
were more. Delivery never raises — a webhook failure (network error, bad
URL, non-2xx response) is logged as a warning and the run's own exit code is
unaffected by it.

## Testing a channel

There's no dedicated `afly test-alert` command yet — the simplest way to
confirm a webhook works is to trigger a real failure deliberately (e.g.
`afly run --select nonexistent_extract` with `error_alerting.enabled: true`
will *not* fire, since a bad selector is a config error before a run starts;
instead point an extract's `apps:` at a bad app id and run it) and check the
channel receives it, then revert.
