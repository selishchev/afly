"""Unit tests for mng_api: pagination, error propagation, and platform filtering."""

from __future__ import annotations

import json

import pytest
import requests_mock

from afly.appsflyer.client import AppsFlyerClient
from afly.appsflyer.errors import AuthError, PermanentError
from afly.appsflyer.mng_api import AppInfo, filter_apps, list_apps
from afly.appsflyer.retry import RetryPolicy

_APPS_URL = "https://hq1.appsflyer.com/api/mng/apps"


def _page(items: list[dict], total_items: int) -> str:
    return json.dumps(
        {"data": items, "meta": {"total_items": total_items}, "links": {"next": None}}
    )


def _app(app_id: str, name: str = "App", platform: str = "android") -> dict:
    return {
        "id": app_id,
        "type": "app",
        "attributes": {"name": name, "platform": platform, "currency": "USD", "time_zone": "UTC"},
    }


@pytest.mark.unit
def test_single_page_under_limit() -> None:
    client = AppsFlyerClient(token="t")
    policy = RetryPolicy(sleep=lambda s: None)

    with requests_mock.Mocker() as m:
        m.get(_APPS_URL, text=_page([_app("com.a.b"), _app("com.c.d")], total_items=2))
        apps = list_apps(client, policy, limit=1000)

    assert len(apps) == 2
    assert all(isinstance(a, AppInfo) for a in apps)


@pytest.mark.unit
def test_pagination_over_two_pages() -> None:
    client = AppsFlyerClient(token="t")
    policy = RetryPolicy(sleep=lambda s: None)

    with requests_mock.Mocker() as m:
        m.get(
            _APPS_URL,
            [
                {"text": _page([_app("com.a"), _app("com.b")], total_items=3)},
                {"text": _page([_app("com.c")], total_items=3)},
            ],
        )
        apps = list_apps(client, policy, limit=2)

    ids = [a.id for a in apps]
    assert sorted(ids) == ["com.a", "com.b", "com.c"]
    assert len(m.request_history) == 2
    assert m.request_history[0].qs["offset"] == ["0"]
    assert m.request_history[1].qs["offset"] == ["2"]


@pytest.mark.unit
def test_pagination_stops_on_short_page_even_without_total_items() -> None:
    client = AppsFlyerClient(token="t")
    policy = RetryPolicy(sleep=lambda s: None)

    with requests_mock.Mocker() as m:
        m.get(_APPS_URL, text=json.dumps({"data": [_app("com.a")], "meta": {}}))
        apps = list_apps(client, policy, limit=1000)

    assert len(apps) == 1
    assert len(m.request_history) == 1


@pytest.mark.unit
def test_results_sorted_by_id() -> None:
    client = AppsFlyerClient(token="t")
    policy = RetryPolicy(sleep=lambda s: None)

    with requests_mock.Mocker() as m:
        m.get(_APPS_URL, text=_page([_app("com.z"), _app("com.a"), _app("com.m")], total_items=3))
        apps = list_apps(client, policy)

    assert [a.id for a in apps] == ["com.a", "com.m", "com.z"]


@pytest.mark.unit
def test_tolerates_missing_attributes() -> None:
    client = AppsFlyerClient(token="t")
    policy = RetryPolicy(sleep=lambda s: None)
    minimal_item = {"id": "com.bare", "type": "app"}  # no "attributes" key at all

    with requests_mock.Mocker() as m:
        m.get(_APPS_URL, text=_page([minimal_item], total_items=1))
        apps = list_apps(client, policy)

    assert apps == [AppInfo(id="com.bare", name="", platform="", currency=None, time_zone=None)]


@pytest.mark.unit
def test_401_raises_auth_error() -> None:
    client = AppsFlyerClient(token="t")
    policy = RetryPolicy(sleep=lambda s: None)

    with requests_mock.Mocker() as m:
        m.get(_APPS_URL, status_code=401, text="unauthorized")
        with pytest.raises(AuthError):
            list_apps(client, policy)


@pytest.mark.unit
def test_404_raises_permanent_error() -> None:
    client = AppsFlyerClient(token="t")
    policy = RetryPolicy(sleep=lambda s: None)

    with requests_mock.Mocker() as m:
        m.get(_APPS_URL, status_code=404, text="not found")
        with pytest.raises(PermanentError):
            list_apps(client, policy)


# --- filter_apps ---------------------------------------------------------


@pytest.mark.unit
def test_filter_apps_none_returns_all() -> None:
    apps = [AppInfo("a", "A", "ios", None, None), AppInfo("b", "B", "android", None, None)]
    assert filter_apps(apps, None) == apps


@pytest.mark.unit
def test_filter_apps_empty_list_returns_all() -> None:
    apps = [AppInfo("a", "A", "ios", None, None)]
    assert filter_apps(apps, []) == apps


@pytest.mark.unit
def test_filter_apps_matches_case_insensitively() -> None:
    apps = [AppInfo("a", "A", "iOS", None, None), AppInfo("b", "B", "android", None, None)]
    assert filter_apps(apps, ["ios"]) == [apps[0]]


@pytest.mark.unit
def test_filter_apps_multiple_platforms() -> None:
    apps = [
        AppInfo("a", "A", "ios", None, None),
        AppInfo("b", "B", "android", None, None),
        AppInfo("c", "C", "web", None, None),
    ]
    assert filter_apps(apps, ["ios", "android"]) == apps[:2]
