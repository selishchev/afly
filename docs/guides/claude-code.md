# Claude Code

afly ships context for [Claude Code](https://claude.com/claude-code) (or any
AI assistant that reads `CLAUDE.md` + `.claude/`) so it can operate a
project natively — configure extracts, size a backfill against the
AppsFlyer quota, and debug a failing run — instead of guessing at afly's
behavior from scratch each session.

## Set it up

```bash
cd my_project
afly init-claude
```

This writes, into the project directory:

- **`CLAUDE.md`** — created if it doesn't exist, or a managed afly section
  injected between HTML-comment markers if it does (any of your own content
  in the file, before or after the markers, is preserved verbatim).
- **`.claude/rules/afly/`** — the reference an assistant reads on demand:
  pipeline overview, full CLI reference, config fields, extract fields, the
  AppsFlyer CSV formats, the idempotent write path, and the quota/scheduling
  math.
- **`.claude/skills/`** — four procedures an assistant follows step by step:
  `afly-setup-project`, `afly-new-extract`, `afly-backfill`,
  `afly-debug-run`.

## Re-run after upgrading

```bash
afly init-claude
```

Idempotent — re-running with no relevant afly upgrade reports everything
`unchanged`. After upgrading the `afly` package, re-run it so the assistant's
reference matches the version actually installed; anything you've added
outside the managed markers is untouched.

## What to ask for

- *"Set up this afly project"* → the `afly-setup-project` skill walks
  through `.env`, `afly validate`, `afly debug`, a `--dry-run`, and the
  first real run.
- *"Add a TikTok extract"* → the `afly-new-extract` skill picks
  `report_type`/`category`/`media_source`, checks for double-counting
  against sibling extracts, and validates the result.
- *"Backfill 2022 onward"* → the `afly-backfill` skill sizes
  `--chunk-days`/`--max-calls` against the AppsFlyer quota and verifies the
  result against `_afly_loads`.
- *"Why did last night's run fail?"* → the `afly-debug-run` skill reads
  `_afly_loads`, distinguishes a quota skip from a real failure from the
  empty-response guard, and uses `afly debug --pull` to check a CSV-shape
  problem.

## Multiple projects in one workspace

`afly init-claude --target-dir <dir>` scaffolds into any directory — point
it at a parent folder holding several afly projects to share one `CLAUDE.md`
across them, or run it per-project if they diverge significantly.

## The managed block is safe to edit around

Only the text between the `<!-- BEGIN afly ... -->` / `<!-- END afly -->`
markers in `CLAUDE.md` is afly's — write anything else above, below, or
between separate sections freely. A re-run refreshes only that block in
place.
