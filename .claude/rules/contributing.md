# Contributing

How to set up, test, lint, and extend **afly**. For internals and design
rationale, see `./architecture.md`.

## Dev setup

Requires **Python 3.10+**.

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev,integration]"
.venv/bin/pre-commit install
```

Extras (`pyproject.toml` `[project.optional-dependencies]`): `dev` (pytest,
ruff, black, mypy, build, pre-commit, detect-secrets), `integration`
(testcontainers for a Docker-backed ClickHouse). The `afly` console script is
wired via `[project.scripts]`.

## Running tests

```bash
.venv/bin/pytest -m "not integration"   # unit — no network/DB
.venv/bin/pytest -m integration          # needs Docker; spins up clickhouse-server:22.11 and :26.3
```

Markers (`unit`/`integration`) are declared in `pyproject.toml`
`[tool.pytest.ini_options]` and applied per-test with `@pytest.mark.unit` /
`@pytest.mark.integration` — there's no directory-based auto-marking, so a
new test file still needs the decorator on each test.

Unit tests mock the ClickHouse/AppsFlyer layers (`tests/unit/fakes.py`,
`run_fakes.py`) — no real network or database calls. Integration tests
(`tests/integration/`) exercise the real `clickhouse_driver` against a
disposable container, including the actual `REPLACE`/`DROP PARTITION`
mechanics `database/writer.py` relies on.

## Lint, format, type-check

```bash
.venv/bin/pre-commit run --all-files
```

Hooks (`.pre-commit-config.yaml`): trailing-whitespace / end-of-file-fixer /
check-yaml / check-added-large-files / check-merge-conflict (basic hygiene) →
**ruff `--fix`** (lint + import sort; `[tool.ruff]`: line length 100, rules
`E`/`W`/`F`/`I`/`B`/`C4`/`UP`, `E501` deferred to black) → **black**
(formatting, line length 100, target py310) → **mypy** (scoped to `^afly/`
via the hook's `files:`) → **detect-secrets** (`.secrets.baseline`).

CI (`.github/workflows/ci.yml`) runs unit tests on 3.10/3.11/3.12, ruff+black
as a hard gate, mypy with `continue-on-error: true` (strict mode is
aspirational, not yet a merge blocker), and integration tests in a separate
job. `.github/workflows/publish.yml` builds and publishes on any `v*` tag
push via PyPI trusted publishing (OIDC — no stored token), asserting
`afly.__version__` matches the tag first.

## Code conventions

- **English only** — code, comments, docstrings, docs (open-source library).
- **Docstrings explain why**, not what — the code already says what.
- **Files stay small and single-purpose** — split by concern well before a
  module reaches ~250 lines (see `run/_fetch.py`/`_rebuild.py`/`_setup.py`,
  split out of `executor.py`/`runner.py`).
- **Pydantic for every config model**, `extra="forbid"` everywhere — a
  typo'd YAML key must fail loudly at load time, never be silently ignored.
- **Naive UTC everywhere** (`afly.utils.datetime_utils`) — never let an
  aware `datetime` reach a call site; see `architecture.md`.
- **afly never mutates a table it didn't create**, and never `ALTER`s a
  destination on its own — a schema mismatch raises
  `SchemaMismatchError` with the exact statement to run by hand.
- **Never `REPLACE PARTITION` from an empty staging table** — silently
  empties the destination partition on ClickHouse 22.11. See
  `database/writer.py`'s docstring and `architecture.md`.
- **Every external dependency is injectable** in the run pipeline
  (`run/runner.py:RunDeps`) — new code that needs the clock, ClickHouse, or
  the AppsFlyer client should take it as a parameter/field, not import and
  call a concrete implementation directly, so it stays testable without
  monkeypatching module internals.

## How to extend

### Add a new AppsFlyer report shape

1. Confirm it's a **by-date** report (has a `Date` column) — anything else
   can't drive the idempotent write path; see the rejection list in
   `config/extract_config.py` (`_UNSUPPORTED_REPORT_TYPES`) and
   `ExtractConfig`'s docstring.
2. Add the `report_type` literal to `ExtractConfig.report_type`.
3. If it introduces new CSV columns, add them to
   `afly.schema.DESTINATION_COLUMNS` (both the parser and the DDL generator
   pick them up automatically) and to `csvmap/headers.py:KNOWN_HEADERS`.
4. Add a fixture CSV under `tests/fixtures/csv/` and a parser test.
5. Update the shipped `afly/cli/assets/claude/rules/formats.md` and
   `docs/guides/formats-and-facebook-split.md` — both describe the real
   shape, not an aspiration.

### Add a new alert channel type

1. Add the `type` literal to `config/profile.py:AlertChannelConfig`.
2. Add the payload-building branch in `alerting/webhook.py:_build_payload`.
3. Add a test exercising `send_failure_alert` against the new type.
4. Update `afly/cli/assets/claude/rules/project.md`'s channel example and
   `docs/guides/alerting.md`.

### Add a new CLI command

1. Implementation module under `cli/commands/<name>.py`, returning an `int`
   exit code (never calling `sys.exit` itself — `cli/main.py` does that).
2. Wire it into `cli/main.py` as a `@cli.command()`, **lazy-importing** the
   implementation inside the callback body (keeps `--help`/`--version`
   instant even for commands with heavy transitive imports).
3. Route offline-only logic through `cli/_project.py:load_project_only`;
   anything needing credentials through `load_context`.
4. Update `afly/cli/assets/claude/rules/cli.md` and
   `docs/reference/cli.md`.

## Release checklist

1. **Bump the version** — `__version__` in `afly/__init__.py` (the only
   source; `pyproject.toml` reads it dynamically via
   `[tool.setuptools.dynamic]`).
2. **Update `CHANGELOG.md`** — Keep a Changelog format.
3. **Update `docs/`** and the `afly init-claude` assets
   (`afly/cli/assets/claude/`) so a freshly-run `afly init-claude` matches
   shipped behavior. Added/removed a shipped rule or skill? Extend
   `tests/unit/test_init_claude.py`'s `RULE_FILES`/`SKILL_FILES` sets too.
4. **Run the gate** — `pytest -m "not integration"` and
   `pre-commit run --all-files` must pass; run `pytest -m integration` if
   Docker is available.
5. **Tag `vX.Y.Z`** and push it — `publish.yml` builds and publishes via
   PyPI trusted publishing, after asserting the tag matches
   `afly.__version__`.

## PR workflow

- Discuss non-trivial changes (a new report shape, a new channel type, a
  config/schema change) before implementing.
- Match existing patterns — funnel config errors through `ConfigError`,
  AppsFlyer errors through the `appsflyer.errors` hierarchy, keep the
  dependency-injection shape of `run/runner.py:RunDeps` for new pipeline
  code.
- All unit tests pass and pre-commit is clean.
- Update `CHANGELOG.md`, `docs/`, and the `afly init-claude` assets when
  behavior changes.
