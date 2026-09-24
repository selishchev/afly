---
name: afly-backfill
description: >-
  Plan and run a wide historical afly backfill without exceeding AppsFlyer's
  daily quota. Use when the user wants to load history for a new extract,
  reload a wide date range after a config change, or asks why a backfill is
  slow / hit a rate limit. Sizes --chunk-days/--max-calls and verifies the
  result against _afly_loads.
---

# Run an afly backfill

Load a wide date range deliberately, sized against AppsFlyer's quota, and
confirm afterward it actually completed. Read
`.claude/rules/afly/quotas.md` for the tier math applied here, and `cli.md`
for exact `afly run` options.

1. **Confirm window and extracts.** Get `--select` and `--from`/`--to` from
   the user (omit `--from` for "everything, from the extracts'
   `start_date`"). Reloading after a config change? Add `--full-refresh` —
   otherwise the watermark skips everything already loaded.

2. **See the real plan first.**
   ```bash
   afly run --select "<sel>" --from <date> --to <date> --dry-run
   ```
   Prints, per extract: apps, window, chunk/long-call counts, quota totals
   (flagged `EXCEEDS` if over budget), estimated wall time. Read it before
   computing anything by hand.

3. **Keep chunks short unless there's a reason not to.** Don't raise
   `--chunk-days` to "go faster" — crossing `quota.long_call_min_days`
   (default 3) moves every chunk into the small **daily** budget tier,
   almost always the slower path for a wide backfill (worked example in
   `quotas.md`: ~18h at `chunk_days: 2` vs ~7 days of budget-limited waiting
   at `chunk_days: 30`, same window). Only widen it if the short-call count
   itself is the bottleneck **and** the dry-run's long-call total still
   fits the daily budget.

4. **Cap the run, don't try it all in one shot.** For more than a day or so
   of estimated time:
   ```bash
   afly run --select "<sel>" --from <date> --to <date> --chunk-days 2 --max-calls 2000 --json
   # repeat; the watermark makes each run continue where the last stopped
   ```
   `--json` gives `exit_code`, `totals.skipped`, and `quota` without
   eyeballing stderr.

5. **Verify against `_afly_loads`, not just the exit code.** Exit 0 covers
   both "fully done" and "stopped by policy":
   ```sql
   SELECT extract, app_id, max(to_date) AS watermark
   FROM _afly_loads WHERE status = 'success'
   GROUP BY extract, app_id ORDER BY extract, app_id;

   SELECT extract, app_id, from_date, to_date, error
   FROM _afly_loads
   WHERE status = 'failed' AND started_at >= now() - INTERVAL 1 DAY
   ORDER BY started_at DESC;
   ```
   Short of the target `--to`? Re-run the same command — it resumes. Any
   `failed` (not `skipped`) rows need the **`afly-debug-run`** skill before
   re-running blind.

6. **Report** the final watermark per extract/app versus the target window,
   total rows loaded, and total API calls spent — from the last run's
   `--json` `totals`/`extracts[]`, or the query above for a multi-run
   backfill.
