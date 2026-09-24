# CLAUDE.md

Guidance for Claude Code (or any AI assistant) working **on afly's own
source** — not on a project that *uses* afly (that context is
`afly init-claude`'s job; see `afly/cli/assets/claude/`).

## What this is

afly is a dbt-style Python CLI + library that idempotently pulls AppsFlyer
aggregate Pull API reports into ClickHouse. Single external API, single
database backend, no plugin system — keep it that simple.

## Architecture map

| Package | Owns |
|---|---|
| `afly/cli/` | `afly` command group, per-command implementations under `commands/`, shared output/`_project` helpers, `assets/claude/` (shipped `init-claude` context). |
| `afly/config/` | Pydantic models/loaders for `afly_project.yml`, `profiles.yml`, `extracts/*.yml`, the `--select` grammar. |
| `afly/appsflyer/` | HTTP client, error classification, retry/backoff, Pull API request builder, app-list API. No config/ClickHouse imports. |
| `afly/csvmap/` | AppsFlyer CSV → `afly.schema.DESTINATION_COLUMNS` row mapping. Pure. |
| `afly/schema.py` | Single source of truth for destination columns — parser and DDL generator both import it. |
| `afly/database/` | ClickHouse driver wrapper, DDL + schema-drift check, the idempotent writer, `_afly_loads`/`_afly_locks` repos, `debug --deep` checks. |
| `afly/run/` | `afly run` pipeline: options → window/chunk planning → quota scheduling → fetch/parse → rebuild → summary/alert. |
| `afly/alerting/` | Run-failure webhook delivery (Mattermost/Slack/generic). |
| `afly/utils/` | Naive-UTC datetime contract, env interpolation, table-name qualification. |

Full detail + the ClickHouse 22.11 findings: `.claude/rules/architecture.md`.

## Conventions

- Files stay small (~250 lines is the soft ceiling) — split by concern.
- Docstrings explain **why**, not what.
- afly never mutates or `ALTER`s a table it didn't create — a schema
  mismatch raises `SchemaMismatchError` with the exact statement to run.
- Never `REPLACE PARTITION` from an empty staging table — silently empties
  the destination partition on ClickHouse 22.11 (`architecture.md`).
- Every internal datetime is naive UTC — never mix in an aware one.
- Tests: `unit` (no network/DB, mocked layers) and `integration`
  (`clickhouse-server:22.11` via testcontainers), marked per `pyproject.toml`.

## Dev commands

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev,integration]"
.venv/bin/pytest -m "not integration"      # unit tests
.venv/bin/pytest -m integration             # needs Docker
.venv/bin/pre-commit run --all-files        # ruff, black, mypy, detect-secrets
```

## Secrets

Never a literal credential in a committed file — `.env` (gitignored) only;
`detect-secrets` runs as a pre-commit hook.

## Release

Bump `afly/__init__.py`'s `__version__`, update `CHANGELOG.md`, tag
`vX.Y.Z`; CI publishes via PyPI trusted publishing (OIDC, no stored token).
Full checklist: `.claude/rules/contributing.md`.
