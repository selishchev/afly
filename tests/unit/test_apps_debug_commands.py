"""Unit tests for afly.cli.commands.apps / debug.

Both command modules import real interfaces from afly.cli._project,
afly.cli._output, and afly.config.discovery (owned by a different
milestone) — rather than mock those modules wholesale, each test
monkeypatches only the specific names the command under test calls
(``load_context``, ``list_apps``, ``load_extracts``, ``fetch_report``),
using plain ``types.SimpleNamespace`` fakes shaped like the real
ProjectContext/ExtractConfig. This keeps the tests independent of whichever
milestone lands ``_project``/``config`` changes next, while still exercising
the command modules' real orchestration logic.
"""

from __future__ import annotations

import json
import types
from datetime import date, timedelta
from pathlib import Path

import pytest

from afly.appsflyer.errors import AppsFlyerError
from afly.appsflyer.mng_api import AppInfo
from afly.appsflyer.pull_api import RawReport
from afly.cli._project import ProjectError
from afly.cli.commands import apps as apps_cmd
from afly.cli.commands import debug as debug_cmd
from afly.config import ConfigError
from afly.utils.datetime_utils import today_utc

_TOKEN = "SECRET_TOKEN_do-not-leak-9f8e7d"


def _fake_ctx(token: str = _TOKEN, max_retries: int = 2, root: Path | None = None) -> object:
    appsflyer = types.SimpleNamespace(
        token=token, base_url="https://hq1.appsflyer.com", timeout_seconds=30, user_agent=None
    )
    clickhouse = types.SimpleNamespace(
        host="ch", port=9000, user="default", password="", database="db"
    )
    profile = types.SimpleNamespace(appsflyer=appsflyer, clickhouse=clickhouse)
    quota = types.SimpleNamespace(
        max_retries=max_retries,
        retry_policy_kwargs=lambda: {
            "transient_base_wait": 30.0,
            "transient_cap": 600.0,
            "retry_jitter": 0.0,
        },
    )
    project = types.SimpleNamespace(
        quota=quota,
        defaults=types.SimpleNamespace(partition_granularity="month"),
        paths=types.SimpleNamespace(extracts="extracts"),
    )
    return types.SimpleNamespace(
        root=root or Path("/tmp/fake-afly-project"),
        project=project,
        profiles=types.SimpleNamespace(),
        profile_name="default",
        profile=profile,
    )


def _fake_extract_config(name: str = "standard", **overrides: object) -> object:
    defaults: dict[str, object] = {
        "name": name,
        "report_type": "geo_by_date_report",
        "category": "standard",
        "media_source": None,
        "reattr": False,
        "attribution_touch_type": None,
        "timezone": None,
        "currency": None,
        "extra_params": {},
        "exclude_media_sources": [],
        "keep_unknown_columns": False,
    }
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _fake_loaded_extract(name: str = "standard", **overrides: object) -> object:
    return types.SimpleNamespace(
        path=Path(f"extracts/{name}.yml"), config=_fake_extract_config(name=name, **overrides)
    )


# --- apps ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fake_clickhouse_checks(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Every debug test gets a passing, connection-free ClickHouse check.

    ``run_debug`` always runs the ClickHouse-side checks (they are part of
    the basic diagnosis, ``--deep`` only widens them); a real driver call
    against the fake profile would fail and mask what each test asserts.
    Records the ``deep`` flags it was called with so tests can assert wiring.
    """
    from afly.database import checks as ch_checks

    calls: dict = {"deep": []}

    def _fake(profile: object, *, deep: bool, granularity: str = "month") -> list:
        calls["deep"].append(deep)
        return [ch_checks.CheckOutcome("ClickHouse reachable", True, "fake ok")]

    monkeypatch.setattr(ch_checks, "run_clickhouse_checks", _fake)
    return calls


@pytest.mark.unit
def test_run_apps_success_prints_table_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(apps_cmd, "load_context", lambda profile: _fake_ctx())
    apps = [
        AppInfo("com.a", "App A", "ios", "USD", "UTC"),
        AppInfo("com.b", "App B", "android", "EUR", "UTC"),
    ]
    monkeypatch.setattr(apps_cmd, "list_apps", lambda client, policy: apps)

    rc = apps_cmd.run_apps(None, None, False)

    assert rc == 0
    out = capsys.readouterr().out
    assert "com.a" in out and "com.b" in out
    assert "Done." in out


@pytest.mark.unit
def test_run_apps_project_error_exits_one(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def _raise(profile: str | None) -> object:
        raise ProjectError("no afly_project.yml found")

    monkeypatch.setattr(apps_cmd, "load_context", _raise)

    rc = apps_cmd.run_apps(None, None, False)

    assert rc == 1
    assert "no afly_project.yml found" in capsys.readouterr().err


@pytest.mark.unit
def test_run_apps_config_error_exits_one(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def _raise(profile: str | None) -> object:
        raise ConfigError("bad yaml")

    monkeypatch.setattr(apps_cmd, "load_context", _raise)

    rc = apps_cmd.run_apps(None, None, False)

    assert rc == 1
    assert "bad yaml" in capsys.readouterr().err


@pytest.mark.unit
def test_run_apps_appsflyer_error_exits_one(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(apps_cmd, "load_context", lambda profile: _fake_ctx())

    def _raise(client: object, policy: object) -> list[AppInfo]:
        raise AppsFlyerError("boom", status=500, body="server error")

    monkeypatch.setattr(apps_cmd, "list_apps", _raise)

    rc = apps_cmd.run_apps(None, None, False)

    assert rc == 1
    err = capsys.readouterr().err
    assert "500" in err


@pytest.mark.unit
def test_run_apps_platform_filter_narrows_results(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(apps_cmd, "load_context", lambda profile: _fake_ctx())
    apps = [
        AppInfo("com.a", "App A", "ios", None, None),
        AppInfo("com.b", "App B", "android", None, None),
    ]
    monkeypatch.setattr(apps_cmd, "list_apps", lambda client, policy: apps)

    rc = apps_cmd.run_apps(None, "ios", False)

    assert rc == 0
    out = capsys.readouterr().out
    assert "com.a" in out
    assert "com.b" not in out


@pytest.mark.unit
def test_run_apps_json_output_is_valid_json(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(apps_cmd, "load_context", lambda profile: _fake_ctx())
    apps = [AppInfo("com.a", "App A", "ios", "USD", "UTC")]
    monkeypatch.setattr(apps_cmd, "list_apps", lambda client, policy: apps)

    rc = apps_cmd.run_apps(None, None, True)

    assert rc == 0
    out = capsys.readouterr().out
    body = out.split("\nDone.")[0]
    payload = json.loads(body)
    assert payload == [
        {"id": "com.a", "name": "App A", "platform": "ios", "currency": "USD", "time_zone": "UTC"}
    ]


@pytest.mark.unit
def test_run_apps_never_leaks_token(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(apps_cmd, "load_context", lambda profile: _fake_ctx(token=_TOKEN))
    monkeypatch.setattr(
        apps_cmd, "list_apps", lambda client, policy: [AppInfo("com.a", "A", "ios", None, None)]
    )

    apps_cmd.run_apps(None, None, True)

    captured = capsys.readouterr()
    assert _TOKEN not in captured.out
    assert _TOKEN not in captured.err


# --- debug: base checks ------------------------------------------------


@pytest.mark.unit
def test_run_debug_all_base_checks_pass(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(debug_cmd, "load_context", lambda profile: _fake_ctx())
    monkeypatch.setattr(
        debug_cmd, "list_apps", lambda client, policy: [AppInfo("com.a", "A", "ios", None, None)]
    )

    rc = debug_cmd.run_debug(None, False, None, None, None, None, False)

    assert rc == 0


@pytest.mark.unit
def test_run_debug_mng_api_check_fails(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(debug_cmd, "load_context", lambda profile: _fake_ctx())

    def _raise(client: object, policy: object) -> list[AppInfo]:
        raise AppsFlyerError("down", status=500, body="oops")

    monkeypatch.setattr(debug_cmd, "list_apps", _raise)

    rc = debug_cmd.run_debug(None, False, None, None, None, None, False)

    assert rc == 1


@pytest.mark.unit
def test_run_debug_project_error_exits_one(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def _raise(profile: str | None) -> object:
        raise ProjectError("missing profiles.yml")

    monkeypatch.setattr(debug_cmd, "load_context", _raise)

    rc = debug_cmd.run_debug(None, False, None, None, None, None, False)

    assert rc == 1
    assert "missing profiles.yml" in capsys.readouterr().err


@pytest.mark.unit
def test_run_debug_passes_deep_flag_to_clickhouse_checks(
    monkeypatch: pytest.MonkeyPatch, capsys, fake_clickhouse_checks
) -> None:
    monkeypatch.setattr(debug_cmd, "load_context", lambda profile: _fake_ctx())
    monkeypatch.setattr(debug_cmd, "list_apps", lambda client, policy: [])

    rc = debug_cmd.run_debug(None, True, None, None, None, None, False)

    assert rc == 0
    assert fake_clickhouse_checks["deep"] == [True]
    assert "ClickHouse reachable: fake ok" in capsys.readouterr().out


@pytest.mark.unit
def test_run_debug_without_deep_still_runs_basic_clickhouse_checks(
    monkeypatch: pytest.MonkeyPatch, fake_clickhouse_checks
) -> None:
    monkeypatch.setattr(debug_cmd, "load_context", lambda profile: _fake_ctx())
    monkeypatch.setattr(debug_cmd, "list_apps", lambda client, policy: [])

    rc = debug_cmd.run_debug(None, False, None, None, None, None, False)

    assert rc == 0
    assert fake_clickhouse_checks["deep"] == [False]


@pytest.mark.unit
def test_run_debug_failing_clickhouse_check_fails_the_command(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from afly.database import checks as ch_checks

    monkeypatch.setattr(debug_cmd, "load_context", lambda profile: _fake_ctx())
    monkeypatch.setattr(debug_cmd, "list_apps", lambda client, policy: [])
    monkeypatch.setattr(
        ch_checks,
        "run_clickhouse_checks",
        lambda profile, *, deep, granularity="month": [
            ch_checks.CheckOutcome("ClickHouse reachable", False, "connection refused")
        ],
    )

    rc = debug_cmd.run_debug(None, True, None, None, None, None, False)

    assert rc == 1
    assert "connection refused" in capsys.readouterr().err


@pytest.mark.unit
def test_run_debug_clickhouse_driver_exception_is_a_failed_check(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from afly.database import checks as ch_checks

    monkeypatch.setattr(debug_cmd, "load_context", lambda profile: _fake_ctx())
    monkeypatch.setattr(debug_cmd, "list_apps", lambda client, policy: [])

    def _boom(profile: object, *, deep: bool, granularity: str = "month") -> list:
        raise OSError("host unreachable")

    monkeypatch.setattr(ch_checks, "run_clickhouse_checks", _boom)

    rc = debug_cmd.run_debug(None, False, None, None, None, None, False)

    assert rc == 1
    assert "OSError: host unreachable" in capsys.readouterr().err


# --- debug: --pull ----------------------------------------------------


@pytest.mark.unit
def test_run_debug_pull_requires_app(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(debug_cmd, "load_context", lambda profile: _fake_ctx())
    monkeypatch.setattr(debug_cmd, "list_apps", lambda client, policy: [])

    rc = debug_cmd.run_debug(None, False, "standard", None, None, None, False)

    assert rc == 1
    assert "--app" in capsys.readouterr().err


@pytest.mark.unit
def test_run_debug_pull_window_too_large_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(debug_cmd, "load_context", lambda profile: _fake_ctx())
    monkeypatch.setattr(debug_cmd, "list_apps", lambda client, policy: [])

    rc = debug_cmd.run_debug(None, False, "standard", "app1", "2026-09-01", "2026-09-05", False)

    assert rc == 1
    assert "2 days" in capsys.readouterr().err


@pytest.mark.unit
def test_run_debug_pull_default_window_is_yesterday() -> None:
    start, end = debug_cmd._resolve_window(None, None)
    yesterday = today_utc() - timedelta(days=1)
    assert start == end == yesterday


@pytest.mark.unit
def test_run_debug_pull_two_day_window_is_allowed() -> None:
    start, end = debug_cmd._resolve_window("2026-09-01", "2026-09-02")
    assert start == date(2026, 9, 1)
    assert end == date(2026, 9, 2)


@pytest.mark.unit
def test_run_debug_pull_three_day_window_raises() -> None:
    with pytest.raises(ValueError, match="2 days"):
        debug_cmd._resolve_window("2026-09-01", "2026-09-03")


@pytest.mark.unit
def test_run_debug_pull_extract_not_found(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(debug_cmd, "load_context", lambda profile: _fake_ctx())
    monkeypatch.setattr(debug_cmd, "list_apps", lambda client, policy: [])
    monkeypatch.setattr(debug_cmd, "load_extracts", lambda root, project: [])

    rc = debug_cmd.run_debug(None, False, "nonexistent", "app1", "2026-09-01", "2026-09-01", False)

    assert rc == 1
    assert "nonexistent" in capsys.readouterr().err


@pytest.mark.unit
def test_run_debug_pull_fetch_error(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(debug_cmd, "load_context", lambda profile: _fake_ctx())
    monkeypatch.setattr(debug_cmd, "list_apps", lambda client, policy: [])
    monkeypatch.setattr(debug_cmd, "load_extracts", lambda root, project: [_fake_loaded_extract()])

    def _raise(*args: object, **kwargs: object) -> RawReport:
        raise AppsFlyerError("nope", status=404, body="unknown app")

    monkeypatch.setattr(debug_cmd, "fetch_report", _raise)

    rc = debug_cmd.run_debug(None, False, "standard", "app1", "2026-09-01", "2026-09-01", False)

    assert rc == 1
    assert "fetch failed" in capsys.readouterr().err


@pytest.mark.unit
def test_run_debug_pull_success_parses_and_summarizes(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(debug_cmd, "load_context", lambda profile: _fake_ctx())
    monkeypatch.setattr(debug_cmd, "list_apps", lambda client, policy: [])
    monkeypatch.setattr(debug_cmd, "load_extracts", lambda root, project: [_fake_loaded_extract()])

    raw = RawReport(
        text="Date,Country\n2026-09-01,US\n",
        status=200,
        url="https://hq1.appsflyer.com/report",
        params={"from": "2026-09-01", "to": "2026-09-01"},
        api_calls=1,
        elapsed_ms=42,
    )
    monkeypatch.setattr(debug_cmd, "fetch_report", lambda *a, **k: raw)

    rc = debug_cmd.run_debug(None, False, "standard", "app1", "2026-09-01", "2026-09-01", False)

    assert rc == 0
    out = capsys.readouterr().out
    assert "status=200" in out
    assert "rows=1" in out


@pytest.mark.unit
def test_run_debug_pull_passes_currency_from_mng_api_when_known(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The --pull check should stamp the app's currency (from the app-list
    API) onto ReportContext, and the human summary should show it."""
    monkeypatch.setattr(debug_cmd, "load_context", lambda profile: _fake_ctx())
    calls = {"n": 0}

    def _list_apps(client: object, policy: object) -> list[AppInfo]:
        calls["n"] += 1
        return [AppInfo("app1", "App One", "ios", "EUR", "UTC")]

    monkeypatch.setattr(debug_cmd, "list_apps", _list_apps)
    monkeypatch.setattr(debug_cmd, "load_extracts", lambda root, project: [_fake_loaded_extract()])

    raw = RawReport(
        text="Date,Country\n2026-09-01,DE\n",
        status=200,
        url="https://hq1.appsflyer.com/report",
        params={"from": "2026-09-01", "to": "2026-09-01"},
        api_calls=1,
        elapsed_ms=42,
    )
    monkeypatch.setattr(debug_cmd, "fetch_report", lambda *a, **k: raw)

    rc = debug_cmd.run_debug(None, False, "standard", "app1", "2026-09-01", "2026-09-01", False)

    assert rc == 0
    out = capsys.readouterr().out
    assert "currency=EUR" in out
    # The base "AppsFlyer mng API reachable" check and the --pull currency
    # lookup share one cached list_apps() call, not two.
    assert calls["n"] == 1


@pytest.mark.unit
def test_run_debug_pull_survives_mng_api_failure_falling_back_to_no_currency(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A --pull check must not fail just because the currency hint (a
    best-effort mng-API lookup) is unreachable — parse_report can still
    detect the currency from the CSV headers on its own."""
    monkeypatch.setattr(debug_cmd, "load_context", lambda profile: _fake_ctx())

    def _raise(client: object, policy: object) -> list[AppInfo]:
        raise AppsFlyerError("down", status=500, body="oops")

    monkeypatch.setattr(debug_cmd, "list_apps", _raise)
    monkeypatch.setattr(debug_cmd, "load_extracts", lambda root, project: [_fake_loaded_extract()])

    raw = RawReport(
        text="Date,Country\n2026-09-01,DE\n",
        status=200,
        url="https://hq1.appsflyer.com/report",
        params={"from": "2026-09-01", "to": "2026-09-01"},
        api_calls=1,
        elapsed_ms=42,
    )
    monkeypatch.setattr(debug_cmd, "fetch_report", lambda *a, **k: raw)

    rc = debug_cmd.run_debug(None, False, "standard", "app1", "2026-09-01", "2026-09-01", False)

    # The base mng-API check fails (rc=1 overall) but the pull itself still
    # ran and printed a result rather than raising.
    assert rc == 1
    out = capsys.readouterr().out
    assert "currency=(none detected)" in out


@pytest.mark.unit
def test_run_debug_json_output_is_valid_json_with_pull(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(debug_cmd, "load_context", lambda profile: _fake_ctx())
    monkeypatch.setattr(
        debug_cmd, "list_apps", lambda client, policy: [AppInfo("com.a", "A", "ios", None, None)]
    )
    monkeypatch.setattr(debug_cmd, "load_extracts", lambda root, project: [_fake_loaded_extract()])
    raw = RawReport(
        text="Date,Country\n2026-09-01,US\n",
        status=200,
        url="https://x",
        params={},
        api_calls=1,
        elapsed_ms=10,
    )
    monkeypatch.setattr(debug_cmd, "fetch_report", lambda *a, **k: raw)

    rc = debug_cmd.run_debug(None, False, "standard", "app1", "2026-09-01", "2026-09-01", True)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["AppsFlyer mng API reachable"]["ok"] is True
    assert payload["pull"]["row_count"] == 1
    assert payload["pull"]["status"] == 200


@pytest.mark.unit
def test_run_debug_never_leaks_token(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(debug_cmd, "load_context", lambda profile: _fake_ctx(token=_TOKEN))
    monkeypatch.setattr(
        debug_cmd, "list_apps", lambda client, policy: [AppInfo("com.a", "A", "ios", None, None)]
    )
    monkeypatch.setattr(debug_cmd, "load_extracts", lambda root, project: [_fake_loaded_extract()])
    raw = RawReport(
        text="Date,Country\n2026-09-01,US\n",
        status=200,
        url="https://x",
        params={},
        api_calls=1,
        elapsed_ms=1,
    )
    monkeypatch.setattr(debug_cmd, "fetch_report", lambda *a, **k: raw)

    debug_cmd.run_debug(None, True, "standard", "app1", "2026-09-01", "2026-09-01", True)

    captured = capsys.readouterr()
    assert _TOKEN not in captured.out
    assert _TOKEN not in captured.err
