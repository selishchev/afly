"""Unit tests for afly.run._apps.build_apps_resolver.

``apps_for`` is exercised more broadly through afly.run.planner/test_runner's
end-to-end plans — this file isolates the resolver itself, in particular the
``currency_for`` companion added for multi-currency support (afly.csvmap):
it shares the same memoized account-wide app-list cache as ``apps_for``, so
whether it can answer at all depends on whether that cache ever got filled.
"""

from __future__ import annotations

import pytest

from afly.appsflyer.mng_api import AppInfo
from afly.run._apps import build_apps_resolver

from .run_fakes import make_loaded


@pytest.mark.unit
def test_currency_for_returns_known_app_currency_after_apps_for_triggers_list() -> None:
    apps = [
        AppInfo(id="app1", name="A", platform="ios", currency="USD", time_zone=None),
        AppInfo(id="app2", name="B", platform="android", currency="EUR", time_zone=None),
    ]
    resolver = build_apps_resolver(lambda: apps)
    extract = make_loaded("standard", apps=None)  # apps: null -> needs the account-wide list

    resolver.apps_for(extract)

    assert resolver.currency_for("app1") == "USD"
    assert resolver.currency_for("app2") == "EUR"


@pytest.mark.unit
def test_currency_for_returns_none_when_list_never_fetched() -> None:
    # An extract that names its own `apps:` never triggers the account-wide
    # list call at all (see build_apps_resolver's docstring) — currency_for
    # then has nothing to answer from, and afly.run._fetch falls back to
    # per-report header detection instead.
    def _boom() -> list[AppInfo]:
        raise AssertionError("list_all_apps should not be called")

    resolver = build_apps_resolver(_boom)
    extract = make_loaded("standard", apps=["app1"])

    apps = resolver.apps_for(extract)

    assert apps == ["app1"]
    assert resolver.currency_for("app1") is None


@pytest.mark.unit
def test_currency_for_returns_none_for_an_unknown_app_id() -> None:
    apps = [AppInfo(id="app1", name="A", platform="ios", currency="USD", time_zone=None)]
    resolver = build_apps_resolver(lambda: apps)
    resolver.apps_for(make_loaded("standard", apps=None))

    assert resolver.currency_for("does-not-exist") is None


@pytest.mark.unit
def test_currency_for_skips_apps_with_no_currency_reported() -> None:
    # AppInfo.currency is Optional — the mng API can omit it.
    apps = [AppInfo(id="app1", name="A", platform="ios", currency=None, time_zone=None)]
    resolver = build_apps_resolver(lambda: apps)
    resolver.apps_for(make_loaded("standard", apps=None))

    assert resolver.currency_for("app1") is None


@pytest.mark.unit
def test_list_all_apps_is_memoized_across_apps_for_and_currency_for() -> None:
    calls = {"n": 0}

    def _list_all_apps() -> list[AppInfo]:
        calls["n"] += 1
        return [AppInfo(id="app1", name="A", platform="ios", currency="EUR", time_zone=None)]

    resolver = build_apps_resolver(_list_all_apps)
    resolver.apps_for(make_loaded("standard", apps=None))
    resolver.apps_for(make_loaded("facebook", apps=None))
    resolver.currency_for("app1")

    assert calls["n"] == 1
