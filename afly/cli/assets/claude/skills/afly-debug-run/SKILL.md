---
name: afly-debug-run
description: >-
  Diagnose a failing or suspicious `afly run` — tell a quota skip from a real
  failure from the empty-response guard, clear a stuck lock, and confirm the
  AppsFlyer CSV shape. Use when a run exited non-zero, a chunk is marked
  failed/skipped in the output, `afly run` refuses to start with a lock
  error, or the loaded data looks wrong/incomplete.
---

# Debug an afly run

Read the evidence (`_afly_loads`, the run's stderr/`--json`) before guessing.
Write-path mechanics: `.claude/rules/afly/idempotency.md`; quota semantics:
`quotas.md`.

1. **Get the run's own record.** With a `run_id` (stderr or `--json`):
   ```sql
   SELECT extract, app_id, from_date, to_date, status, rows, api_calls,
          http_status, error, skip_reason
   FROM _afly_loads WHERE run_id = '<run_id>' ORDER BY started_at;
   ```
   Otherwise, the most recent attempts (`ORDER BY started_at DESC LIMIT 50`).

2. **Classify what you see.**
   - `skipped`, reason starts `"quota:"` — not a failure; a daily/per-app
     budget was hit or AppsFlyer rate-limited. Next scheduled run resumes
     from the watermark. Recurring → the project's `quota:` numbers may not
     match the account's real contract; confirm with the user first.
   - `skipped`, reason starts `"empty response guard:"` — AppsFlyer returned
     zero rows for a day that already had data; afly refused to apply it
     (`on_empty: skip`, default). Often transient — re-run later before
     assuming real data loss; only investigate if it persists across
     several runs.
   - `failed` — read `error`. A `PermanentError` (400/404) usually means a
     bad app id or misconfiguration (`afly ls`/`afly apps`). A ClickHouse
     error means the rebuild step broke — `afly debug --deep` reproduces
     the same mechanics on disposable tables.
   - `running` with no matching finish row — still in flight, or its
     process died. No quota drawn, no write happened — safe to re-run.

3. **"Failed to acquire lock" / a run won't start.**
   ```bash
   afly unlock --table <db.table>   # or --all
   ```
   A lock only outlives its run on a crash — it also auto-expires after
   `lock_timeout_seconds` (default 7200s). Check the reported
   `owner`/`age_seconds` first — unlocking a genuinely still-running lock
   risks two writers on the same table. `--force` on `afly run` skips the
   check instead of clearing it; prefer `afly unlock` so the decision is
   explicit.

4. **The data looks wrong (not missing, just wrong).** Double-counted spend
   on a media source → check `afly validate`'s ownership warning
   (`extracts.md`). Facebook missing campaign/adset/adgroup → the pull
   wasn't Facebook-scoped, or `category`/`media_source` needs adjusting
   (step 5). A `NULL`/empty value → likely a normal AppsFlyer null token
   (`formats.md`), not a bug.

5. **Confirm the actual CSV shape.**
   ```bash
   afly debug --pull <extract> --app <id> [--from D --to D]
   ```
   Non-destructive, no ClickHouse write. Reports the detected `format`, the
   real `headers`, `unknown_headers` (consider `keep_unknown_columns: true`
   while investigating), and sample rows.

6. **Report** which chunks are genuinely failed (need action) versus
   skipped by policy (self-resolving), the fix applied if any, and the exact
   re-run command — usually just `afly run --select <extract>` again.
