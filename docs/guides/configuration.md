# Configuration

An afly project has two YAML files at its root, plus one file per extract
under `extracts/` (see [Extracts](extracts.md)). Exhaustive field-by-field
tables live in [Config reference](../reference/config.md) — this guide
explains the shape and the choices that matter.

## `afly_project.yml`

Committed to git. No credentials live here — only structure and defaults.

```yaml
name: my_project
version: "1.0"
default_profile: prod             # must name a profile defined in profiles.yml

paths:
  extracts: extracts              # where extracts/*.yml live

defaults:                         # project-wide fallback for optional extract fields
  start_date: 2024-01-01
  lookback_days: 3                # re-pull the last N closed days on every run
  chunk_days: 2                   # keep AppsFlyer calls in the per-minute tier — see quotas.md
  include_current_day: true
  currency: preferred              # AppsFlyer ignores this for these reports — see below
  on_empty: skip                  # never wipe a day because AppsFlyer returned empty
  keep_unknown_columns: false

quota:                            # AppsFlyer Pull API budget afly enforces on itself
  account_long_calls_per_day: 120
  app_long_calls_per_day: 24

lock_timeout_seconds: 7200

error_alerting:
  enabled: false
  channels: [ops_mattermost]
```

`defaults` fields fall through to every extract that doesn't override them —
an extract only states what makes it different (see
[Extracts](extracts.md)). `defaults.start_date` has no built-in fallback: set
it here, or on every extract individually, or extract loading fails with a
named error telling you which.

**`currency` doesn't do what it looks like it does.** It's a real AppsFlyer
Pull API query param, but AppsFlyer ignores it for the aggregate geo reports
afly pulls and always returns each app's own currency regardless (verified
live, 2026-09-23) — a `currency: USD` project pulling a EUR app still gets
EUR money columns. afly records whichever currency the response actually
came in as the destination `currency` column (see
[Formats guide](formats-and-facebook-split.md#currency)); it does not
convert anything. Convert downstream if your marts need one currency across
apps.

## `profiles.yml`

Holds credentials and connection details, and is meant to be **environment-
specific** — kept out of source control in most teams' setups, or committed
with every secret field as an env-var reference (never a literal value).

```yaml
default_profile: prod

profiles:
  prod:
    appsflyer:
      token: "{{ env_var('APPSFLYER_TOKEN') }}"
    clickhouse:
      host: "{{ env_var('CLICKHOUSE_HOST') }}"
      protocol: native                            # "native" (default, clickhouse-driver) or "http" (clickhouse-connect)
      port: 9000                                  # optional — defaults to the protocol's own port (native 9000, http 8123)
      user: "{{ env_var('CLICKHOUSE_USER') }}"
      password: "{{ env_var('CLICKHOUSE_PASSWORD') }}"
      database: appsflyer

alert_channels:
  ops_mattermost:
    type: mattermost
    webhook_url: "{{ env_var('MATTERMOST_WEBHOOK_URL') }}"
```

### Environment interpolation

Two equivalent syntaxes are recognized anywhere a string value appears:

```yaml
password: "${CLICKHOUSE_PASSWORD}"                # shell-style
password: "{{ env_var('CLICKHOUSE_PASSWORD') }}"  # dbt-style
```

A variable that's set but empty resolves to an empty string; one that isn't
set at all leaves the literal placeholder in the loaded config — which is a
**hard error** for every command that actually connects (`run`, `debug`,
`apps`, `unlock`), so a missing credential fails loudly naming the variable,
rather than silently sending `${CLICKHOUSE_PASSWORD}` as a password. `afly
validate` is the exception: it treats the same condition as a warning, so it
can pass in CI before secrets are provisioned.

### ClickHouse transport: `native` vs `http`

`clickhouse.protocol` picks how afly talks to ClickHouse — same warehouse,
two wire protocols:

- **`native`** (default) — `clickhouse-driver`, TCP, port 9000 by default.
- **`http`** — `clickhouse-connect`, HTTP(S), port 8123 by default.

Which one to use is an operational fact about the network path to that
warehouse, not a data-shape choice: both protocols hit the same tables with
identical results (see `tests/integration/test_clickhouse_layer.py`, which
runs the whole suite against both). Use `http` when the native port is
firewalled off or doesn't answer the handshake from where afly runs (a common
symptom: the TCP connection opens but nothing ever completes the protocol
exchange) — a production worker with direct network access to the cluster
can typically stay on `native`, while a developer or analyst machine behind a
stricter network boundary may only be able to reach the HTTP port.

`port` is optional either way — unset, it follows `protocol` (native
9000/9440, http 8123/8443, the `44*0` variants when `secure: true`); set it
explicitly only to override.

### Multiple profiles

A project can define several profiles — e.g. `prod` and `staging` against
different ClickHouse clusters, or different AppsFlyer accounts — and pick one
per invocation:

```bash
afly run --select "*" --profile staging
```

## Where things point

| Setting | What it's for |
|---|---|
| `paths.extracts` | Where `extracts/*.yml` are discovered from. |
| `tables.loads` / `tables.locks` | Names of afly's own bookkeeping tables (`_afly_loads`/`_afly_locks` by default). |
| `profiles.<name>.clickhouse.database` | Where destination tables (from each extract's `table:`) live by default. |
| `profiles.<name>.clickhouse.internal_database` | Where `_afly_loads`/`_afly_locks` live — defaults to the same as `database`. |
| `staging_database` | the destination's db | Database for transient `…__afly_staging` tables; set it when the destination db is mirrored automatically. |
| `error_alerting.channels` | Names looked up in `profiles.yml`'s `alert_channels` — see [Alerting](alerting.md). |

Full field list with every default: [Config reference](../reference/config.md).
