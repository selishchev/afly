"""Unit tests for afly.run.runner.run_pipeline — the end-to-end orchestration.

Builds a small, fully self-contained afly project per test (fixed
``start_date``, an extract with an explicit ``apps:`` list so no AppsFlyer
management-API call is ever needed) rather than relying on wall-clock
"today" for window math, and injects every RunDeps seam with fakes — no
real network or ClickHouse connection anywhere in this file.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from afly.appsflyer.errors import AppsFlyerError
from afly.cli._project import ProjectError, load_context
from afly.database.ddl import SchemaMismatchError
from afly.database.locks import LockHeldError
from afly.run.options import RunOptions
from afly.run.runner import RunDeps, run_pipeline
from afly.run.summary import RunSummary

from .run_fakes import FakeAppsFlyer, FakeLoadsRepo, FakeLocksRepo, FakeManager, FakeRebuilder

_REPORT_TYPE = "geo_by_date_report"
_DAY = date(2026, 1, 5)

_PROJECT_YML = """\
name: testproj
version: "1.0"
default_profile: prod

paths:
  extracts: extracts

tables:
  loads: _afly_loads
  locks: _afly_locks

defaults:
  start_date: 2026-01-01
  lookback_days: 3
  chunk_days: 2
  include_current_day: true
  currency: preferred
  on_empty: skip
  keep_unknown_columns: false

lock_timeout_seconds: 7200

error_alerting:
  enabled: {alerting_enabled}
  channels: [ops]
"""

_PROFILES_YML = """\
default_profile: prod

profiles:
  prod:
    appsflyer:
      token: "{{ env_var('APPSFLYER_TOKEN') }}"
    clickhouse:
      host: "{{ env_var('CLICKHOUSE_HOST') }}"
      port: 9000
      user: "{{ env_var('CLICKHOUSE_USER') }}"
      password: "{{ env_var('CLICKHOUSE_PASSWORD') }}"
      database: appsflyer

alert_channels:
  ops:
    type: mattermost
    webhook_url: "{{ env_var('MATTERMOST_WEBHOOK_URL') }}"
"""

_EXTRACT_YML = """\
name: standard
report_type: geo_by_date_report
apps: ["app1"]
table: appsflyer_geo_by_date
"""


def _write_project(tmp_path: Path, *, alerting_enabled: bool = False) -> Path:
    root = tmp_path / "proj"
    (root / "extracts").mkdir(parents=True)
    (root / "afly_project.yml").write_text(
        _PROJECT_YML.format(alerting_enabled=str(alerting_enabled).lower())
    )
    (root / "profiles.yml").write_text(_PROFILES_YML)
    (root / "extracts" / "standard.yml").write_text(_EXTRACT_YML)
    return root


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPSFLYER_TOKEN", "test-token")
    monkeypatch.setenv("CLICKHOUSE_HOST", "localhost")
    monkeypatch.setenv("CLICKHOUSE_USER", "default")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "test-pass")
    monkeypatch.setenv("MATTERMOST_WEBHOOK_URL", "https://example.com/hook")


def _options(**overrides: object) -> RunOptions:
    base: dict[str, object] = {
        "select": "standard",
        "from_date": _DAY,
        "to_date": _DAY,
        "chunk_days": 1,
    }
    base.update(overrides)
    return RunOptions(**base)  # type: ignore[arg-type]


def _summary(options: RunOptions) -> RunSummary:
    return RunSummary(
        selector=options.select, exclude=options.exclude, started_at=datetime(2026, 1, 5)
    )


def _deps(
    root: Path,
    *,
    client: FakeAppsFlyer,
    loads: FakeLoadsRepo,
    locks: FakeLocksRepo,
    rebuilders: dict[tuple[str, str], FakeRebuilder] | None = None,
    alert_calls: list[dict[str, Any]] | None = None,
    alert_result: bool = True,
) -> RunDeps:
    rebuilders = rebuilders if rebuilders is not None else {}
    alert_calls = alert_calls if alert_calls is not None else []

    def _rebuilder_factory(manager: Any, db: str, table: str, granularity: str) -> Any:
        return rebuilders.setdefault((db, table), FakeRebuilder(granularity=granularity))

    def _alert_sender(channel: Any, **kwargs: Any) -> bool:
        alert_calls.append({"channel": channel, **kwargs})
        return alert_result

    return RunDeps(
        load_context=lambda profile=None: load_context(profile, start=root),
        manager_factory=lambda profile: FakeManager(),
        client_factory=lambda profile: client,
        list_apps=lambda client_, policy: [],
        loads_repo_factory=lambda manager, db, table: loads,
        locks_repo_factory=lambda manager, db, table: locks,
        rebuilder_factory=_rebuilder_factory,
        ensure_internal_tables=lambda *a, **k: None,
        ensure_destination=lambda *a, **k: False,
        now=lambda: datetime(2026, 1, 5, 12, 0, 0),
        today=lambda: _DAY,
        clock=lambda: 0.0,
        sleep=lambda s: None,
        alert_sender=_alert_sender,
    )


# -- config errors -----------------------------------------------------


@pytest.mark.unit
def test_config_error_returns_1_without_alert(tmp_path: Path) -> None:
    missing_root = tmp_path / "nope"
    options = _options()
    summary = _summary(options)
    alert_calls: list[dict[str, Any]] = []
    deps = _deps(
        missing_root,
        client=FakeAppsFlyer(),
        loads=FakeLoadsRepo(),
        locks=FakeLocksRepo(),
        alert_calls=alert_calls,
    )

    rc = run_pipeline(options, summary=summary, deps=deps)

    assert rc == 1
    assert summary.status == "error"
    assert summary.error is not None
    assert alert_calls == []


@pytest.mark.unit
def test_load_context_raising_project_error_is_a_config_error(tmp_path: Path) -> None:
    root = _write_project(tmp_path)
    options = _options()
    summary = _summary(options)

    def _raise_load_context(profile: str | None = None) -> Any:
        raise ProjectError("boom: unresolved env var FOO")

    deps = _deps(root, client=FakeAppsFlyer(), loads=FakeLoadsRepo(), locks=FakeLocksRepo())
    deps.load_context = _raise_load_context

    rc = run_pipeline(options, summary=summary, deps=deps)
    assert rc == 1
    assert "boom" in (summary.error or "")


# -- locking --------------------------------------------------------------


@pytest.mark.unit
def test_lock_held_returns_1(tmp_path: Path) -> None:
    root = _write_project(tmp_path)
    options = _options()
    summary = _summary(options)

    held_error = LockHeldError(
        "table:appsflyer.appsflyer_geo_by_date",
        "otherhost:999",
        "run-xyz",
        datetime(2026, 1, 1),
        999.0,
    )
    locks = FakeLocksRepo(held={"table:appsflyer.appsflyer_geo_by_date": held_error})
    deps = _deps(root, client=FakeAppsFlyer(), loads=FakeLoadsRepo(), locks=locks)

    rc = run_pipeline(options, summary=summary, deps=deps)

    assert rc == 1
    assert summary.status == "error"
    assert "otherhost:999" in (summary.error or "")


@pytest.mark.unit
def test_lock_held_alerts_when_enabled(tmp_path: Path) -> None:
    root = _write_project(tmp_path, alerting_enabled=True)
    options = _options()
    summary = _summary(options)

    held_error = LockHeldError(
        "table:appsflyer.appsflyer_geo_by_date",
        "otherhost:999",
        "run-xyz",
        datetime(2026, 1, 1),
        999.0,
    )
    locks = FakeLocksRepo(held={"table:appsflyer.appsflyer_geo_by_date": held_error})
    alert_calls: list[dict[str, Any]] = []
    deps = _deps(
        root, client=FakeAppsFlyer(), loads=FakeLoadsRepo(), locks=locks, alert_calls=alert_calls
    )

    rc = run_pipeline(options, summary=summary, deps=deps)

    assert rc == 1
    assert len(alert_calls) == 1
    # The friendlier, operator-facing message is echoed, but the alert/
    # summary.error still carry the raw exception text (see runner._fail).
    assert "otherhost:999" in summary.error


@pytest.mark.unit
def test_schema_mismatch_returns_1_and_alerts_when_enabled(tmp_path: Path) -> None:
    root = _write_project(tmp_path, alerting_enabled=True)
    options = _options()
    summary = _summary(options)

    locks = FakeLocksRepo()
    alert_calls: list[dict[str, Any]] = []
    deps = _deps(
        root, client=FakeAppsFlyer(), loads=FakeLoadsRepo(), locks=locks, alert_calls=alert_calls
    )

    def _ensure_destination_raises(*a: Any, **k: Any) -> Any:
        raise SchemaMismatchError("appsflyer.appsflyer_geo_by_date: missing column `foo`")

    deps.ensure_destination = _ensure_destination_raises

    rc = run_pipeline(options, summary=summary, deps=deps)

    assert rc == 1
    assert "missing column" in (summary.error or "")
    assert len(alert_calls) == 1


@pytest.mark.unit
def test_load_extracts_config_error_alerts_when_enabled(tmp_path: Path) -> None:
    root = _write_project(tmp_path, alerting_enabled=True)
    # A second extract with the same `name:` as "standard" makes load_extracts
    # raise ConfigError (duplicate idempotency/selector key).
    (root / "extracts" / "dup.yml").write_text(_EXTRACT_YML)
    options = _options()
    summary = _summary(options)
    alert_calls: list[dict[str, Any]] = []
    deps = _deps(
        root,
        client=FakeAppsFlyer(),
        loads=FakeLoadsRepo(),
        locks=FakeLocksRepo(),
        alert_calls=alert_calls,
    )

    rc = run_pipeline(options, summary=summary, deps=deps)

    assert rc == 1
    assert summary.status == "error"
    assert len(alert_calls) == 1


@pytest.mark.unit
def test_empty_selector_match_alerts_when_enabled(tmp_path: Path) -> None:
    root = _write_project(tmp_path, alerting_enabled=True)
    options = _options(select="no_such_extract")
    summary = _summary(options)
    alert_calls: list[dict[str, Any]] = []
    deps = _deps(
        root,
        client=FakeAppsFlyer(),
        loads=FakeLoadsRepo(),
        locks=FakeLocksRepo(),
        alert_calls=alert_calls,
    )

    rc = run_pipeline(options, summary=summary, deps=deps)

    assert rc == 1
    assert "no_such_extract" in (summary.error or "")
    assert len(alert_calls) == 1


@pytest.mark.unit
def test_appsflyer_error_resolving_apps_alerts_when_enabled(tmp_path: Path) -> None:
    root = _write_project(tmp_path, alerting_enabled=True)
    # apps: null (unset) so apps_for() triggers the account-wide list call.
    (root / "extracts" / "standard.yml").write_text(
        "name: standard\nreport_type: geo_by_date_report\ntable: appsflyer_geo_by_date\n"
    )
    options = _options()
    summary = _summary(options)
    alert_calls: list[dict[str, Any]] = []
    deps = _deps(
        root,
        client=FakeAppsFlyer(),
        loads=FakeLoadsRepo(),
        locks=FakeLocksRepo(),
        alert_calls=alert_calls,
    )

    def _list_apps_raises(client_: Any, policy: Any) -> Any:
        raise AppsFlyerError("boom", status=500, body="server error")

    deps.list_apps = _list_apps_raises

    rc = run_pipeline(options, summary=summary, deps=deps)

    assert rc == 1
    assert "AppsFlyer request failed" in (summary.error or "")
    assert len(alert_calls) == 1


@pytest.mark.unit
def test_dry_run_never_alerts_even_with_alerting_enabled(tmp_path: Path) -> None:
    """A bona fide exit-0/dry-run path must never alert, regardless of what
    `error_alerting.enabled` says — only an actual failure does."""
    root = _write_project(tmp_path, alerting_enabled=True)
    options = _options(dry_run=True)
    summary = _summary(options)
    alert_calls: list[dict[str, Any]] = []
    deps = _deps(
        root,
        client=FakeAppsFlyer(),
        loads=FakeLoadsRepo(),
        locks=FakeLocksRepo(),
        alert_calls=alert_calls,
    )

    rc = run_pipeline(options, summary=summary, deps=deps)

    assert rc == 0
    assert alert_calls == []


@pytest.mark.unit
def test_dry_run_that_fails_before_planning_does_not_alert(tmp_path: Path) -> None:
    """Exit paths that sit before the dry-run return (here: nothing matches the
    selector) still exit 1 under --dry-run, but a dry run never pages anyone."""
    root = _write_project(tmp_path, alerting_enabled=True)
    options = _options(dry_run=True, select="no_such_extract")
    summary = _summary(options)
    alert_calls: list[dict[str, Any]] = []
    deps = _deps(
        root,
        client=FakeAppsFlyer(),
        loads=FakeLoadsRepo(),
        locks=FakeLocksRepo(),
        alert_calls=alert_calls,
    )

    rc = run_pipeline(options, summary=summary, deps=deps)

    assert rc == 1
    assert alert_calls == []


# -- dry run --------------------------------------------------------------


@pytest.mark.unit
def test_dry_run_returns_0_and_never_acquires_or_fetches(tmp_path: Path) -> None:
    root = _write_project(tmp_path)
    options = _options(dry_run=True)
    summary = _summary(options)

    client = FakeAppsFlyer()
    locks = FakeLocksRepo()
    deps = _deps(root, client=client, loads=FakeLoadsRepo(), locks=locks)

    rc = run_pipeline(options, summary=summary, deps=deps)

    assert rc == 0
    assert summary.status == "dry_run"
    assert locks.acquired == []
    assert client.calls == []


# -- success / cleanup -----------------------------------------------------


@pytest.mark.unit
def test_success_releases_locks_and_drops_staging(tmp_path: Path) -> None:
    root = _write_project(tmp_path)
    options = _options()
    summary = _summary(options)

    client = FakeAppsFlyer()
    client.script(_REPORT_TYPE, "app1", _DAY, _DAY, "Date,Installs\n2026-01-05,10\n")
    locks = FakeLocksRepo()
    rebuilders: dict[tuple[str, str], FakeRebuilder] = {}
    deps = _deps(root, client=client, loads=FakeLoadsRepo(), locks=locks, rebuilders=rebuilders)

    rc = run_pipeline(options, summary=summary, deps=deps)

    assert rc == 0
    assert summary.status == "success"
    assert locks.acquired  # a lock was taken...
    assert len(locks.released) == len(locks.acquired)  # ...and released
    assert rebuilders  # a rebuilder was created...
    assert all(r.staging_dropped for r in rebuilders.values())  # ...and staging dropped


@pytest.mark.unit
def test_cleanup_happens_even_when_executor_raises(tmp_path: Path) -> None:
    root = _write_project(tmp_path)
    options = _options()
    summary = _summary(options)

    # No scripted response for app1's window -> FakeAppsFlyer.get() raises
    # AssertionError, simulating an unmodeled crash inside the executor.
    client = FakeAppsFlyer()
    locks = FakeLocksRepo()
    rebuilders: dict[tuple[str, str], FakeRebuilder] = {}
    deps = _deps(root, client=client, loads=FakeLoadsRepo(), locks=locks, rebuilders=rebuilders)

    with pytest.raises(AssertionError):
        run_pipeline(options, summary=summary, deps=deps)

    assert locks.acquired
    assert len(locks.released) == len(locks.acquired)
    assert rebuilders
    assert all(r.staging_dropped for r in rebuilders.values())


# -- alerting ---------------------------------------------------------------


@pytest.mark.unit
def test_alert_sent_once_when_enabled_and_run_failed(tmp_path: Path) -> None:
    root = _write_project(tmp_path, alerting_enabled=True)
    options = _options()
    summary = _summary(options)

    client = FakeAppsFlyer()
    client.script(_REPORT_TYPE, "app1", _DAY, _DAY, (401, "unauthorized"))
    locks = FakeLocksRepo()
    alert_calls: list[dict[str, Any]] = []
    deps = _deps(root, client=client, loads=FakeLoadsRepo(), locks=locks, alert_calls=alert_calls)

    rc = run_pipeline(options, summary=summary, deps=deps)

    assert rc == 1
    assert summary.aborted == "auth"
    assert len(alert_calls) == 1
    assert alert_calls[0]["channel"].type == "mattermost"


@pytest.mark.unit
def test_no_alert_when_enabled_but_run_succeeded(tmp_path: Path) -> None:
    root = _write_project(tmp_path, alerting_enabled=True)
    options = _options()
    summary = _summary(options)

    client = FakeAppsFlyer()
    client.script(_REPORT_TYPE, "app1", _DAY, _DAY, "Date,Installs\n2026-01-05,1\n")
    locks = FakeLocksRepo()
    alert_calls: list[dict[str, Any]] = []
    deps = _deps(root, client=client, loads=FakeLoadsRepo(), locks=locks, alert_calls=alert_calls)

    rc = run_pipeline(options, summary=summary, deps=deps)

    assert rc == 0
    assert alert_calls == []


@pytest.mark.unit
def test_no_alert_when_failure_but_alerting_disabled(tmp_path: Path) -> None:
    root = _write_project(tmp_path, alerting_enabled=False)
    options = _options()
    summary = _summary(options)

    client = FakeAppsFlyer()
    client.script(_REPORT_TYPE, "app1", _DAY, _DAY, (401, "unauthorized"))
    locks = FakeLocksRepo()
    alert_calls: list[dict[str, Any]] = []
    deps = _deps(root, client=client, loads=FakeLoadsRepo(), locks=locks, alert_calls=alert_calls)

    rc = run_pipeline(options, summary=summary, deps=deps)

    assert rc == 1
    assert alert_calls == []
