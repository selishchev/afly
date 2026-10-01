# Alerting

afly can notify a webhook channel when a run **fails** — a run-level failure
alert, not a data-quality one. It fires **at most once per run**.

## When it fires

Once the project (`afly_project.yml` + `profiles.yml`) has loaded, **every**
exit-code-1 outcome fires the alert (if `error_alerting.enabled`):

- at least one chunk ended `failed`;
- the run aborted early (an authentication failure, or a ClickHouse
  partition-rebuild failure);
- a destination-table lock is held by another run;
- an existing destination table doesn't match afly's schema
  (`SchemaMismatchError`);
- the extract configs failed to load (a bad `extracts/*.yml`, a duplicate
  `name:`, a `partition_granularity` conflict across extracts sharing one
  `table:`);
- the selector matched no *enabled* extract;
- resolving an extract's app list hit an AppsFlyer error (the management-API
  call `apps: null` triggers).

It never fires for:

- a quota/policy skip (a rate limit, `--max-calls`/`--max-minutes`, the
  empty-response guard, a disabled extract) — those are expected outcomes,
  recorded `skipped`, not failures;
- `--dry-run` — nothing was attempted;
- a genuinely empty window (`plan.jobs` is empty — "nothing to do").

**The one case it can't cover**: if the project or `profiles.yml` itself
fails to load (missing file, bad YAML, an unresolved credential env var),
there is no `ErrorAlertingConfig` to even check yet — that surfaces only as
a non-zero exit code for your scheduler to catch (see
[Scheduling](scheduling.md)).

## Enable it

```yaml
# afly_project.yml
error_alerting:
  enabled: true
  channels: [ops_mattermost]
  mentions: ["@oncall"]           # optional, passed through to the channel;
                                   # written with or without the leading "@" —
                                   # both are normalized to exactly one
```

```yaml
# profiles.yml
alert_channels:
  ops_mattermost:
    type: mattermost
    webhook_url: "{{ env_var('MATTERMOST_WEBHOOK_URL') }}"
    channel: alerts                # optional
    username: afly                 # optional, default "afly"
    run_url: "${PREFECT_UI_BASE_URL}/runs/flow-run/${PREFECT__FLOW_RUN_ID}"
                                    # optional — see "The run link" below
```

`error_alerting.channels` names must exist in `profiles.yml`'s
`alert_channels` — an unknown name is skipped with a warning, not a hard
failure (so a typo doesn't crash an otherwise-successful-looking run).

## Channel types

| `type` | Delivery |
|---|---|
| `mattermost` | Incoming webhook, native "attachments" payload. |
| `slack` | Same "attachments" shape — Slack's incoming webhooks accept it. |
| `webhook` | Generic — `{"title", "text", "project", "run_id", "run_url", "mentions"}` JSON body, for anything else (PagerDuty relay, a custom endpoint, …). |

Every channel type shares `webhook_url` (required), `channel`, `username`
(default `afly`), `icon_emoji`, `timeout` (default 10s), and `run_url`
(default `""`). Full field list: [Config reference](../reference/config.md).

## What the alert contains

A one-line summary (`afly run failed: <project>/<profile>`), the abort
reason if the run aborted, up to 10 failed chunks
(`extract app_id from..to: error`) with a "... and N more" tail if there were
more, the run link (if resolved — see below), and the mentions.

**Where each piece lives depends on the channel type:**

- **`mattermost`/`slack`** — the summary/abort/failed-chunk lines, the run
  link, and the mentions are all one block: the top-level message `text` is
  always empty, and everything lives inside the colored attachment's own
  `text`, in this order — the summary lines, a blank line, then the run link
  (if resolved) immediately followed by the mentions (if any) as the very
  last line. A webhook/Slack message with content split across the
  top-level `text` and the attachment reads as two disconnected messages;
  folding it all into the attachment avoids that.
- **`webhook`** — a flat JSON object: `title`, `text` (the summary/abort/
  failed-chunk lines joined with `\n`), `project`, `run_id`, `run_url`
  (string or `null`), `mentions` (a list, each normalized to one leading
  `@`).

Delivery never raises — a webhook failure (network error, bad URL, non-2xx
response) is logged as a warning and the run's own exit code is unaffected
by it.

## The run link (`run_url`)

`alert_channels.<name>.run_url` is a link to *this invocation's* run in
whatever orchestrates `afly run` — typically written with env placeholders
so it resolves per-invocation, e.g. a Prefect flow-run URL:

```yaml
run_url: "${PREFECT_UI_BASE_URL}/runs/flow-run/${PREFECT__FLOW_RUN_ID}"
```

If it's empty, or a placeholder is still unresolved after env interpolation
(a laptop run has no orchestrator, so `PREFECT__FLOW_RUN_ID` is simply never
set there), the link is **silently omitted** from the payload — never a
config error, never a warning. This is deliberate: most local/manual runs
have no orchestrator at all, and nagging about a link that will never exist
on a laptop would just be noise.

## Mentions

`error_alerting.mentions` may be written with or without a leading `@` —
`oncall` and `@oncall` both normalize to exactly `@oncall` in the payload.
They're the attachment's last line (Mattermost/Slack) or the `mentions` list
(`webhook`) — see "What the alert contains" above.

## Testing a channel

There's no dedicated `afly test-alert` command yet — the simplest way to
confirm a webhook works is to trigger a real failure deliberately, e.g.
point an extract's `apps:` at a bad app id and run it with
`error_alerting.enabled: true`, then check the channel receives it and
revert. A bad `--select` works too now that it alerts (it didn't in earlier
versions) — but an extract-config error or an empty selector match are less
representative of a real production failure than an actual AppsFlyer/
ClickHouse error, so prefer one of those for a realistic test.
