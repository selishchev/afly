# Quotas & scheduling

AppsFlyer's Pull API enforces two independent rate limits. afly's scheduler
is built to respect both automatically — this page explains the tiers, and
how to size a wide backfill so it doesn't fight the daily budget.

## The two tiers

| Tier | Scope | Applies to | Limit |
|---|---|---|---|
| **Short call** | per app + report type | any pull spanning fewer than `quota.long_call_min_days` days (default: < 3) | one call per `short_call_interval_seconds` (default 65s — AppsFlyer's own per-minute window still 403s on exactly-60s spacing) — **no daily cap**. |
| **Long call** | per account, and per app | any pull spanning `quota.long_call_min_days`+ days | `account_long_calls_per_day` (default 120) and `app_long_calls_per_day` (default 24). |

Every extract pulls in `chunk_days`-sized windows (default **2**) — which is
why the default stays in the unlimited short-call tier. Raising `chunk_days`
to 3 or more moves every pull into the small daily budget.

## Why smaller chunks are usually *faster*, not slower

It's tempting to widen `chunk_days` for a backfill, thinking fewer, bigger
calls finish sooner. In practice the opposite tends to be true, because the
daily long-call budget dominates once you cross the threshold.

**Worked example** — 11 apps × 3 extracts sharing one report type, loading 2
years of history (~730 days):

- **`chunk_days: 2`** (default): ~365 chunks per (extract, app) pair × 33
  pairs ≈ **12,000 calls**, all short. With one key per app (extracts on the
  same report type share a key) round-robinned at 65s each, throughput is
  ~10 calls/minute → **about 20 hours** wall time, no daily cap to wait out.
- **`chunk_days: 30`**: ~25 chunks per pair × 33 pairs ≈ **800 calls**, all
  long. Bound by the 120/day account budget → **about a week** before the
  budget lets the backfill finish, even though far fewer calls were needed.

Fewer calls isn't the same as faster when a daily cap is the bottleneck. Stay
at the default `chunk_days: 2` for backfills unless a `--dry-run` shows the
short-call count itself is the limiting factor.

## Sizing a backfill

```bash
afly run --select "*" --from 2022-01-01 --dry-run
```

Always check this first — it prints the exact per-extract chunk counts, the
account/app long-call totals against budget (flagged `EXCEEDS` if over), and
an estimated wall time, so you don't have to compute any of the above by
hand. The estimate is a lower bound (~2s per request, or the busiest key's
own throttle, whichever is larger) — it doesn't model rate-limit retries.

For anything spanning more than a day or so of estimated time, cap the run
and let it resume:

```bash
afly run --select "*" --from 2022-01-01 --chunk-days 2 --max-calls 2000 --json
# re-run the same command; the watermark picks up where it left off
```

## What happens when a limit is hit

- A long job that would exceed the account or app budget is recorded
  `skipped` (not `failed`) — no AppsFlyer call is even attempted.
- An actual AppsFlyer rate-limit response (403/429) doesn't block the run:
  the job is **deferred** — put back on its own `(app_id, report_type)`
  key's queue with an escalating delay (65s, 130s, 195s, ... at the
  defaults) — while every other app's jobs keep being scheduled in the
  meantime, **including jobs from the next plan wave** (see "Wave
  lookahead" below) — not just other keys within the same wave. Only once
  every admitted key is throttled does the run actually wait. After
  `quota.max_retries` deferrals of the same job, it's recorded `skipped`;
  if it was a long job, that app is marked exhausted for the rest of the
  run (every further long job for it is skipped without retrying).
- A transient AppsFlyer failure (5xx, a network error, a read timeout, or a
  retryable 404/408/416/425) doesn't block the run either: the job is
  **deferred** the same way a rate-limited job is — put back on its own key's
  queue with an escalating delay (30s, 60s, 120s, 240s at the defaults) —
  while every other app's jobs keep being scheduled in the meantime. Unlike a
  rate-limit deferral, exhausting `quota.max_retries` attempts ends the chunk
  **failed** (not skipped) — a persistent 5xx is a real failure, not a quota
  condition — except a 403 that never carried AppsFlyer's rate-limit marker,
  which aborts the whole run as an authentication failure once exhausted,
  same as the inline retry policy used by `afly apps`/`afly debug`.
- `--max-calls`/`--max-minutes` stop scheduling entirely once hit; every
  remaining job is `skipped`.

None of the above fails the run (`afly run`'s exit code stays 0, except the
transient-exhaustion case just above, which fails that chunk like any other
failure) — check `_afly_loads.skip_reason`, or the `--json` `quota`/`jobs[]`
fields, to see what was deferred and simply run again later.

## Wave lookahead

A plan is executed **wave by wave** — every chunk sharing the same
epoch-aligned date window, across every extract, so a day's ClickHouse
partition is rebuilt only once every extract's contribution to it has been
fetched. Within one wave, a throttled key never blocks the others (the
deferral behaviour above). Across waves, `quota.max_waves_in_flight`
(default **8**) controls how far ahead the scheduler is allowed to look:
it may hand out jobs from that many waves at once, oldest first, so a
handful of rate-limited keys in one wave don't leave *every other key in
the whole run* idle just because they happen to be the last jobs left in
that particular wave — this was a real observed incident: an 18-key, 54-job
wave that should take ~3 minutes took 21 minutes because one key was
rate-limited 5 times and the other 17 keys had nothing left to do until it
gave up.

Two things stay true regardless of the lookahead depth:

- **Rebuilds still happen strictly in wave order.** Wave *N+1*'s day is
  never rebuilt before wave *N*'s, even if wave *N+1*'s jobs all finish
  fetching first — its rows just sit buffered until its own turn.
- **A pair's chunks are still fetched in order.** A later chunk of the same
  `(extract, app)` pair is never dispatched while an earlier chunk of that
  pair is still pending or cooling down from a rate limit — otherwise a
  later chunk that succeeds before an earlier one's eventual failure would
  leave rows that should have been discarded once the earlier chunk failed.

Set `quota.max_waves_in_flight: 1` to go back to the old strict
one-wave-at-a-time order exactly (useful mainly for reproducing/debugging
an ordering-sensitive issue) — it's a legitimate value, not a deprecated
fallback.

Why the default is **8**, not a smaller number like 2: measured on a live
18-app backfill (12 waves), admitting only 2 waves at a time still left each
key with at most ~6 jobs queued up — once those were used up (one call per
~65s), every key waited for the oldest wave to close behind a single
deferred job, and 59 of the run's 95 minutes were spent idle that way. A
deeper lookahead window keeps other keys busy while one key backs off. The
cost is memory, not time: every admitted wave's fetched rows sit buffered
until that wave is rebuilt, so raise or lower this mainly for unusually large
accounts — a regular run (a handful of waves) is fully admitted either way
regardless of the setting.

## Tuning the budget itself

`quota:` in `afly_project.yml` is a budget **afly enforces on itself** —
it isn't read from AppsFlyer. Set it to match your account's actual limits
if they differ from the defaults (some accounts have higher published
limits). See [Config reference](../reference/config.md) for every field.

## Retry & backoff

Every AppsFlyer call goes through a retry policy for transient failures (5xx,
a network error, a read timeout, or a 403 without AppsFlyer's rate-limit
marker). How that backoff is spent depends on the caller:

- **Inside `afly run`**: a transient failure is deferred through the
  scheduler exactly like a rate-limit hit — see "What happens when a limit is
  hit" above — so it never blocks other apps/keys while it backs off.
- **`afly apps`/`afly debug`, and app-list resolution**: there's no per-key
  scheduler to defer into, so the retry policy sleeps inline, in process,
  between attempts.

Either way, the nominal wait doubles each attempt —
`quota.transient_base_wait_seconds` (default **30**) × 2^(attempt−1) —
capped at `quota.transient_max_wait_seconds` (default **600**). At the
defaults that's 30s/60s/120s/240s before `quota.max_retries` (5) gives up.
Every retry/deferral wait (this one, the inline rate-limit wait, and the
scheduler's own deferral above) is then spread by `quota.retry_jitter`
(default **0.25**): `actual = nominal * (1 + retry_jitter * U)`, `U` uniform
in `[0, 1)`. Jitter only ever *lengthens* a wait, never shortens it, so it can
never undercut AppsFlyer's own per-minute spacing — the point is to stop
several keys (or several afly processes) that all got rate-limited together
from retrying at the exact same instant.

Lower `transient_base_wait_seconds`/`transient_max_wait_seconds` if your
account's outages tend to be short-lived and you'd rather fail faster; raise
them if AppsFlyer-side hiccups on your account routinely outlast a few
minutes.
