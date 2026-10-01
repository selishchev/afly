# afly — AppsFlyer quota & scheduling

AppsFlyer's Pull API enforces two independent rate limits per account. afly's
`quota:` block (`afly_project.yml`) is a **budget afly enforces on itself** to
stay under them — not a value read from AppsFlyer — so it should match your
account's actual contract.

## The two tiers

| Tier | Scope | Applies to | Limit |
|---|---|---|---|
| **Short call** | per `(app_id, report_type)` | any chunk `< quota.long_call_min_days` days (default: < 3) | one call per `short_call_interval_seconds` (default **65s** — AppsFlyer's own per-minute window still 403s on exactly-60s spacing) — **no daily cap**. |
| **Long call** | per account, and per app | any chunk `>= quota.long_call_min_days` days | `account_long_calls_per_day` (default 120) and `app_long_calls_per_day` (default 24), each minus `reserve_long_calls` headroom. |

A chunk's tier is decided purely by its own day span
(`chunk.days = (to_date - from_date).days + 1`), which can be **shorter** than
the configured `chunk_days` at the edges of a window (the last chunk of a
backfill is whatever's left). `chunk_days: 2` (the scaffold default) means
every chunk stays under the 3-day long-call threshold — **this is why it's
the default**: unlimited short calls beat a small daily long-call budget for
sustained/scheduled pulls.

## The scheduler (`QuotaScheduler`)

Draws jobs from up to `quota.max_waves_in_flight` **waves** at once (every
chunk sharing the same epoch-aligned index, across every extract; default
**8**, oldest wave first — see "Wave lookahead" below):

- Jobs are grouped by **key** = `(app_id, report_type)` — note this is
  *not* per-extract: several extracts pulling the same `report_type` for the
  same app (e.g. the scaffold's `standard`/`facebook`/`yandex`, all
  `geo_by_date_report`) share one key and its 65s spacing. More apps, not
  more extracts on the same report type, is what buys parallelism.
- **Round-robins** across ready keys so a run never idles while *any*
  admitted key is ready — it only sleeps when every admitted key is
  currently throttled.
- Before handing out a **long** job, checks both budgets; if either is
  exhausted, the job is recorded `skipped` (`"quota: daily long-call budget
  exhausted (account)"` / `"... (app <id>)"`) rather than attempted.
- A rate-limit response (403 with the AppsFlyer "Limit reached for" marker,
  or a plain 429) does **not** block the run: the job is **deferred** —
  `QuotaScheduler.defer` re-queues it into its own key's queue (kept sorted
  by admission wave, so a same-pair job from a later wave never jumps ahead
  of it — see "Wave lookahead") with an escalating delay (65s, 130s, 195s,
  ... at the defaults, mirroring the old inline `RetryPolicy` backoff
  schedule), so `next_job()` keeps handing out *other* keys — including
  ready keys from the next admitted wave — while this one cools down instead
  of the whole run idling on it. Only once every admitted key is throttled
  does the scheduler actually sleep. After `quota.max_retries` deferrals of
  the *same* job it's recorded `skipped` (`"quota: rate limited after N
  attempts"`), and if it was a long job, marks that **app** exhausted for
  the rest of the run — every further long job for that app, in this wave
  and every later one, is skipped immediately without retrying against
  AppsFlyer again.
- A transient failure (5xx, a network error, a read timeout, or a retryable
  404/408/416/425) **also doesn't block the run**: `QuotaScheduler.defer_transient`
  re-queues it the same way `defer` re-queues a rate-limited job (same
  admission-order queue, same "other keys keep going" effect), but with its
  own per-job attempt counter and AppsFlyer's *transient* backoff schedule
  (30s, 60s, 120s, 240s at the defaults — see "Retry/backoff" below) instead
  of the rate-limit one. Unlike a rate-limit deferral, exhausting
  `quota.max_retries` attempts here ends the chunk **failed**, not `skipped`
  — a persistent 5xx is a genuine failure — except a 403 that never carried
  the rate-limit marker, which aborts the whole run as an authentication
  failure once exhausted (same parity rule the inline retry policy applies —
  see "Retry/backoff").
- `--max-calls`/`--max-minutes` stop scheduling entirely once hit; every
  remaining job (this wave and later ones) is recorded `skipped` with that
  reason. The watermark makes the next `afly run` continue exactly where this
  one stopped — nothing is lost, nothing is duplicated.

## Wave lookahead

`quota.max_waves_in_flight` (default **8**, `ge=1`) is the executor's
cross-wave lookahead depth. `1` reproduces the old strict
one-wave-at-a-time order exactly: every job of a wave dispatches (and that
wave rebuilds) before the next wave's jobs are even admitted to the
scheduler. Raising it off `1` fixed a real observed stall: an 18-key,
54-job wave that should take ~3 minutes took 21 minutes because one key
(`hafl.voice.chat`) was rate-limited 5 times — with only one wave ever
admitted, the other 17 keys had nothing left to dispatch once their own
wave-0 jobs were done, even though a whole next wave of ready work was
sitting right behind it. (The default went `1` → `2` → `8` across two
separate incidents — see "Why the default is 8" below for the second one.)

Two invariants hold regardless of the lookahead depth:

- **Rebuilds stay strictly wave-ordered.** A wave is rebuilt only once
  every one of its own jobs is terminal, and wave *N+1* is never rebuilt
  before wave *N* — even if wave *N+1*'s jobs all resolve first (dispatch
  order across the window is deliberately *not* wave order; rebuild order
  still is). Rows fetched for a not-yet-front wave stay buffered
  (`(db, table, day)`-keyed, same as always) until that wave's own turn.
- **A pair's chunks stay in order.** `ChunkJob.key` is a function of
  `ChunkJob.pair`'s app id and the extract's report type, so a pair's chunks
  always land in the same key's queue — `QuotaScheduler._enqueue` keeps
  that queue sorted by admission wave, so a later chunk of the same
  `(extract, app)` pair is never dispatched while an earlier chunk of that
  pair is still pending or cooling down. Once the earlier chunk resolves,
  the existing pair-failure check (`Executor._failed_pairs`) still applies
  normally — a failed earlier chunk skips the later one with `"skipped
  (earlier chunk failed)"`, exactly as within a single wave.

Why the default is 8, not 2 (measured on a live 18-app backfill, 12 waves):
with 2 waves in flight each key had at most ~6 jobs admitted, used them up at
one call per ~65 s, and then every key waited for the oldest wave to close
behind a single deferred job — 59 of 95 minutes were idle gaps. A deeper
window keeps other keys busy while one key backs off. The cost is memory:
every admitted wave's fetched rows sit in `Executor._buffers` until that wave
is rebuilt — for aggregate reports that is a few thousand rows per wave, so
raise or lower it only for unusually large accounts. A regular run (≤ 4
waves) is fully admitted either way.

None of this ever fails the run by itself (`afly run`'s exit code 0 covers
quota/policy skips) — check `_afly_loads.skip_reason` or the `--json`
`jobs[]`/`quota` fields to see what got deferred.

## Budget arithmetic — worked example

11 apps × 3 extracts sharing one `report_type` × a 2-year backfill
(~730 days):

**At the default `chunk_days: 2`** (short-call tier): 730 days / 2 ≈ 365
chunks per (extract, app) pair × 33 pairs ≈ **12,000 short calls**. Only 11
distinct keys exist (one per app — the 3 extracts share a key per app), each
throttled to one call/65s, round-robinned in parallel → ≈ 10 calls/minute
aggregate throughput → 12,000 / 10 ≈ 1,200 minutes ≈ **~20 hours** wall time,
no daily cap to wait out.

**At `chunk_days: 30`** (crosses into the long-call tier): 730 / 30 ≈ 25
chunks per pair × 33 pairs ≈ **800 long calls**. Bound by the **account**
budget (120/day, well below what the per-app budget of 24×11=264/day would
allow if apps ran independently) → 800 / 120 ≈ **~7 days** before the budget
lets the backfill finish, even though each individual call is "faster" (fewer
requests).

**This is why short chunks are faster for a backfill**, not just gentler on
the API: the long-call daily cap is the actual bottleneck once `chunk_days`
crosses the 3-day threshold, and it dominates regardless of how few total
calls a bigger chunk size needs.

## Sizing a run

- Leave `chunk_days` at the default (2) unless you have a specific reason —
  `afly validate` warns if an extract's `chunk_days >= long_call_min_days`.
- For a wide backfill, prefer `afly run --chunk-days 2 --max-calls N` (small
  chunks, capped call count) over a large `--chunk-days` — see
  `afly-backfill` skill for the recipe and how to size `--max-calls` against
  `afly ls`/`afly run --dry-run`'s printed totals.
- `--dry-run` prints the exact plan: per-extract chunk/long-call counts, the
  account/app long-call totals against budget (flagged `EXCEEDS` if over),
  and an estimated wall time — the larger of the busiest key's own queue
  depth × `short_call_interval_seconds`, and `total_jobs × 2s` (the executor
  is single-threaded, so that's a floor regardless of how shallow any one
  key is). Neither term models rate-limit deferrals, so treat it as a lower
  bound — always run it before a wide backfill.
- `reserve_long_calls` reduces both budgets by a fixed headroom (e.g. leave 10
  calls/day for a human using the AppsFlyer UI concurrently) — set it, don't
  hand-subtract it from `account_long_calls_per_day` yourself, since it also
  applies per-app.

## Retry/backoff (independent of the scheduler above)

`RetryPolicy` (`afly/appsflyer/retry.py`) governs what happens *within one
HTTP attempt sequence*. `afly run` builds it with `defer_rate_limits=True`
and `defer_transient=True` (set in `afly/run/runner.py`), which changes how
`RateLimitError`/`TransientError` are handled — everything else is the same
for every caller (`apps`, `debug`, `run` alike). Every caller that has a
loaded project also passes `quota.transient_base_wait_seconds`/
`transient_max_wait_seconds`/`retry_jitter` into it
(`QuotaConfig.retry_policy_kwargs()`) — a `RetryPolicy` built with no project
at all falls back to the same numbers as `QuotaConfig`'s own defaults (30s /
600s / 0.25), so the two can never drift apart:

- `RateLimitError` (403 "Limit reached for" marker, or 429):
  - **`defer_rate_limits=True`** (`afly run`'s Pull API calls only): raised
    immediately on the first hit — no inline sleep. It's
    `QuotaScheduler.defer` (see above), not `RetryPolicy`, that decides what
    happens next: re-queue with a delay, or skip after `quota.max_retries`
    deferrals. `defer`'s own wait is jittered the same way (below).
  - **`defer_rate_limits=False`** (the default — `afly apps`/`afly debug`,
    which have no per-key scheduler to defer into): waits
    `max(Retry-After, rate_limit_base_wait) × attempt` before retrying —
    linear backoff (60s, 120s, 180s, … at defaults), up to
    `quota.max_retries` attempts, then re-raised.
- `TransientError` (5xx, network/timeout, a read timeout, a retryable
  404/408/416/425, or a 403 *without* the quota marker): exponential backoff
  — `transient_base_wait_seconds × 2^(attempt-1)` (default base **30s**),
  capped at `transient_max_wait_seconds` (default **600s**) — i.e.
  30s/60s/120s/240s before `max_retries` (5) gives up at the defaults.
  - **`defer_transient=True`** (`afly run`'s Pull API calls only): raised
    immediately on the first hit — no inline sleep, no 403→auth escalation
    (that decision needs the attempt count accumulated *across*
    re-dispatches, which only the scheduler tracks). It's
    `QuotaScheduler.defer_transient` (see above), not `RetryPolicy`, that
    decides what happens next: re-queue with the same exponential schedule,
    or give up after `quota.max_retries` attempts — at which point the
    `Executor` finishes the job as a failed chunk, or as the same
    bare-403-escalates-to-auth-abort outcome described below. This is the
    0.2.1 fix for a single-threaded run stalling on one chunk's backoff with
    every other app/key idle (observed in production: 13+ minutes stalled on
    one chunk) — before it, a `TransientError` always retried inline inside
    `afly run` too.
  - **`defer_transient=False`** (the default — `afly apps`/`afly debug`,
    which have no per-key scheduler to defer into): retries inline, in
    process, between attempts — there's no per-key scheduling benefit to
    deferring a 5xx for these callers, since there's no other key's work to
    interleave with it. After `max_retries` attempts, a bare 403 that never
    resolved into a real quota response is re-raised as an **auth failure**
    instead — retrying indefinitely can't distinguish "still throttled" from
    "token revoked," and a fresh 403 that repeats identically at every
    attempt is far more likely the latter.
- **Jitter**: every wait above (the inline rate-limit wait, the transient
  backoff — inline or deferred — and `QuotaScheduler.defer`'s own wait) is
  multiplied by `1 + retry_jitter * U` (`U` uniform in `[0, 1)` via an
  injectable `rand`, default `random.random`) — default `retry_jitter` is
  **0.25**. This only ever lengthens a wait (never below the nominal value),
  so it can't undercut AppsFlyer's per-minute spacing; it exists so several
  keys/processes rate-limited together don't all retry at the exact same
  instant.
- `AuthError` (401, or the escalated 403 above — inline or deferred)
  **aborts the whole run** immediately — no further chunks are attempted,
  `summary.aborted = "auth"`.
- `PermanentError` (400/404) and `EmptyBodyError` fail just that chunk, no
  retry — retrying a bad app id or a malformed date range can't ever succeed.
