"""Unit tests for AppsFlyerClient — headers, redirects, network errors, token masking."""

from __future__ import annotations

import pytest
import requests
import requests_mock

from afly import __version__
from afly.appsflyer.client import AppsFlyerClient
from afly.appsflyer.errors import TransientError


@pytest.mark.unit
def test_sends_bearer_auth_and_accept_headers() -> None:
    client = AppsFlyerClient(token="secret-token-123")
    with requests_mock.Mocker() as m:
        m.get("https://hq1.appsflyer.com/api/mng/apps", text="{}")
        client.get("/api/mng/apps", accept="application/json")

    request = m.request_history[0]
    assert request.headers["authorization"] == "Bearer secret-token-123"
    assert request.headers["accept"] == "application/json"
    assert request.headers["user-agent"] == f"afly/{__version__}"


@pytest.mark.unit
def test_custom_user_agent_overrides_default() -> None:
    client = AppsFlyerClient(token="t", user_agent="my-custom-agent/1.0")
    with requests_mock.Mocker() as m:
        m.get("https://hq1.appsflyer.com/api/mng/apps", text="{}")
        client.get("/api/mng/apps")

    assert m.request_history[0].headers["user-agent"] == "my-custom-agent/1.0"


@pytest.mark.unit
def test_base_url_trailing_slash_is_stripped() -> None:
    client = AppsFlyerClient(token="t", base_url="https://hq1.appsflyer.com/")
    assert client.base_url == "https://hq1.appsflyer.com"


@pytest.mark.unit
def test_get_follows_redirects() -> None:
    client = AppsFlyerClient(token="t")
    with requests_mock.Mocker() as m:
        m.get(
            "https://hq1.appsflyer.com/api/agg-data/export/app/x/geo_by_date_report/v5",
            status_code=302,
            headers={"Location": "https://reports.appsflyer.com/final"},
        )
        m.get("https://reports.appsflyer.com/final", status_code=200, text="Date\n2026-09-01\n")

        response = client.get("/api/agg-data/export/app/x/geo_by_date_report/v5", accept="text/csv")

    assert response.status_code == 200
    assert response.url == "https://reports.appsflyer.com/final"
    assert response.text == "Date\n2026-09-01\n"


@pytest.mark.unit
def test_params_are_forwarded() -> None:
    client = AppsFlyerClient(token="t")
    with requests_mock.Mocker() as m:
        m.get("https://hq1.appsflyer.com/api/mng/apps", text="{}")
        client.get("/api/mng/apps", params={"limit": 1000, "offset": 0})

    assert m.request_history[0].qs == {"limit": ["1000"], "offset": ["0"]}


@pytest.mark.unit
def test_api_calls_counter_increments_per_call() -> None:
    client = AppsFlyerClient(token="t")
    with requests_mock.Mocker() as m:
        m.get("https://hq1.appsflyer.com/api/mng/apps", text="{}")
        client.get("/api/mng/apps")
        client.get("/api/mng/apps")

    assert client.api_calls == 2


@pytest.mark.unit
def test_reset_counter_zeroes_api_calls() -> None:
    client = AppsFlyerClient(token="t")
    with requests_mock.Mocker() as m:
        m.get("https://hq1.appsflyer.com/api/mng/apps", text="{}")
        client.get("/api/mng/apps")

    client.reset_counter()
    assert client.api_calls == 0


@pytest.mark.unit
def test_network_error_becomes_transient_error_and_still_counts() -> None:
    client = AppsFlyerClient(token="t")
    with requests_mock.Mocker() as m:
        m.get(
            "https://hq1.appsflyer.com/api/mng/apps",
            exc=requests.exceptions.ConnectionError("boom"),
        )
        with pytest.raises(TransientError):
            client.get("/api/mng/apps")

    assert client.api_calls == 1


@pytest.mark.unit
def test_timeout_becomes_transient_error() -> None:
    client = AppsFlyerClient(token="t")
    with requests_mock.Mocker() as m:
        m.get(
            "https://hq1.appsflyer.com/api/mng/apps", exc=requests.exceptions.Timeout("timed out")
        )
        with pytest.raises(TransientError):
            client.get("/api/mng/apps")


@pytest.mark.unit
def test_repr_masks_token() -> None:
    client = AppsFlyerClient(token="super-secret-token-value")
    text = repr(client)
    assert "super-secret-token-value" not in text
    assert "…" in text


@pytest.mark.unit
def test_repr_handles_empty_token() -> None:
    client = AppsFlyerClient(token="")
    assert "(empty)" in repr(client)
