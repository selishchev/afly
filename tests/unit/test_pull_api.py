"""Unit tests for pull_api: request building (pure) and fetch_report (HTTP + retry)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest
import requests_mock

from afly.appsflyer.client import AppsFlyerClient
from afly.appsflyer.errors import AuthError, EmptyBodyError, PermanentError
from afly.appsflyer.pull_api import PullRequestSpec, RawReport, build_pull_request, fetch_report
from afly.appsflyer.retry import RetryPolicy

_BASE_URL = "https://hq1.appsflyer.com"
_FROM = date(2026, 9, 1)
_TO = date(2026, 9, 2)


# --- build_pull_request ------------------------------------------------


@pytest.mark.unit
def test_minimal_spec_produces_minimal_params() -> None:
    spec = PullRequestSpec(report_type="geo_by_date_report")
    path, params = build_pull_request(spec, "com.x.y", _FROM, _TO, _BASE_URL)

    assert path == "/api/agg-data/export/app/com.x.y/geo_by_date_report/v5"
    assert params == {"from": "2026-09-01", "to": "2026-09-02"}


@pytest.mark.unit
def test_category_omitted_when_standard() -> None:
    spec = PullRequestSpec(report_type="geo_by_date_report", category="standard")
    _, params = build_pull_request(spec, "app1", _FROM, _TO, _BASE_URL)
    assert "category" not in params


@pytest.mark.unit
def test_category_included_when_not_standard() -> None:
    spec = PullRequestSpec(report_type="geo_by_date_report", category="facebook")
    _, params = build_pull_request(spec, "app1", _FROM, _TO, _BASE_URL)
    assert params["category"] == "facebook"


@pytest.mark.unit
def test_media_source_included_when_set() -> None:
    spec = PullRequestSpec(report_type="geo_by_date_report", media_source="googleadwords_int")
    _, params = build_pull_request(spec, "app1", _FROM, _TO, _BASE_URL)
    assert params["media_source"] == "googleadwords_int"


@pytest.mark.unit
def test_reattr_only_included_when_true() -> None:
    spec_false = PullRequestSpec(report_type="geo_by_date_report", reattr=False)
    _, params_false = build_pull_request(spec_false, "app1", _FROM, _TO, _BASE_URL)
    assert "reattr" not in params_false

    spec_true = PullRequestSpec(report_type="geo_by_date_report", reattr=True)
    _, params_true = build_pull_request(spec_true, "app1", _FROM, _TO, _BASE_URL)
    assert params_true["reattr"] == "true"


@pytest.mark.unit
def test_attribution_touch_type_only_included_when_impression() -> None:
    spec_click = PullRequestSpec(report_type="geo_by_date_report", attribution_touch_type="click")
    _, params_click = build_pull_request(spec_click, "app1", _FROM, _TO, _BASE_URL)
    assert "attribution_touch_type" not in params_click

    spec_impr = PullRequestSpec(
        report_type="geo_by_date_report", attribution_touch_type="impression"
    )
    _, params_impr = build_pull_request(spec_impr, "app1", _FROM, _TO, _BASE_URL)
    assert params_impr["attribution_touch_type"] == "impression"


@pytest.mark.unit
def test_timezone_and_currency_included_when_set() -> None:
    spec = PullRequestSpec(
        report_type="geo_by_date_report", timezone="America/New_York", currency="USD"
    )
    _, params = build_pull_request(spec, "app1", _FROM, _TO, _BASE_URL)
    assert params["timezone"] == "America/New_York"
    assert params["currency"] == "USD"


@pytest.mark.unit
def test_currency_preferred_is_omitted() -> None:
    spec = PullRequestSpec(report_type="geo_by_date_report", currency="preferred")
    _, params = build_pull_request(spec, "app1", _FROM, _TO, _BASE_URL)
    assert "currency" not in params


@pytest.mark.unit
def test_extra_params_are_merged() -> None:
    spec = PullRequestSpec(report_type="geo_by_date_report", extra_params={"foo": "bar"})
    _, params = build_pull_request(spec, "app1", _FROM, _TO, _BASE_URL)
    assert params["foo"] == "bar"


@pytest.mark.unit
@pytest.mark.parametrize("key", ["from", "to"])
def test_extra_params_cannot_override_from_to(key: str) -> None:
    spec = PullRequestSpec(report_type="geo_by_date_report", extra_params={key: "2099-01-01"})
    with pytest.raises(ValueError, match=key):
        build_pull_request(spec, "app1", _FROM, _TO, _BASE_URL)


@pytest.mark.unit
def test_from_extract_reads_attributes_via_getattr() -> None:
    @dataclass
    class FakeExtract:
        report_type: str = "geo_by_date_report"
        category: str = "facebook"
        media_source: str | None = "facebook"
        reattr: bool = True
        attribution_touch_type: str | None = "impression"
        timezone: str | None = "UTC"
        currency: str | None = "USD"
        extra_params: dict[str, str] | None = None

    spec = PullRequestSpec.from_extract(FakeExtract())
    assert spec.report_type == "geo_by_date_report"
    assert spec.category == "facebook"
    assert spec.media_source == "facebook"
    assert spec.reattr is True
    assert spec.attribution_touch_type == "impression"
    assert spec.timezone == "UTC"
    assert spec.currency == "USD"
    assert spec.extra_params == {}


@pytest.mark.unit
def test_from_extract_defaults_missing_optional_attributes() -> None:
    @dataclass
    class MinimalExtract:
        report_type: str = "daily_report"

    spec = PullRequestSpec.from_extract(MinimalExtract())
    assert spec.report_type == "daily_report"
    assert spec.category == "standard"
    assert spec.media_source is None
    assert spec.reattr is False


# --- fetch_report --------------------------------------------------------


def _report_url(app_id: str = "app1", report_type: str = "geo_by_date_report") -> str:
    return f"{_BASE_URL}/api/agg-data/export/app/{app_id}/{report_type}/v5"


@pytest.mark.unit
def test_fetch_report_success_decodes_body_and_counts_calls() -> None:
    client = AppsFlyerClient(token="t")
    spec = PullRequestSpec(report_type="geo_by_date_report")
    policy = RetryPolicy(sleep=lambda s: None)

    with requests_mock.Mocker() as m:
        m.get(_report_url(), status_code=200, text="Date,Country\n2026-09-01,US\n")
        report = fetch_report(client, spec, "app1", _FROM, _TO, policy)

    assert isinstance(report, RawReport)
    assert report.status == 200
    assert report.text == "Date,Country\n2026-09-01,US\n"
    assert report.api_calls == 1
    assert report.params == {"from": "2026-09-01", "to": "2026-09-02"}


@pytest.mark.unit
def test_fetch_report_follows_redirect() -> None:
    client = AppsFlyerClient(token="t")
    spec = PullRequestSpec(report_type="geo_by_date_report")
    policy = RetryPolicy(sleep=lambda s: None)

    with requests_mock.Mocker() as m:
        m.get(
            _report_url(), status_code=302, headers={"Location": "https://reports.appsflyer.com/x"}
        )
        m.get("https://reports.appsflyer.com/x", status_code=200, text="Date\n2026-09-01\n")
        report = fetch_report(client, spec, "app1", _FROM, _TO, policy)

    assert report.status == 200
    assert report.url == "https://reports.appsflyer.com/x"


@pytest.mark.unit
def test_fetch_report_decodes_utf8_bom() -> None:
    client = AppsFlyerClient(token="t")
    spec = PullRequestSpec(report_type="geo_by_date_report")
    policy = RetryPolicy(sleep=lambda s: None)
    body = b"\xef\xbb\xbfDate,Country\n2026-09-01,US\n"  # UTF-8 BOM + ASCII body

    with requests_mock.Mocker() as m:
        m.get(_report_url(), status_code=200, content=body)
        report = fetch_report(client, spec, "app1", _FROM, _TO, policy)

    assert report.text.startswith("Date,Country")
    assert "﻿" not in report.text


@pytest.mark.unit
def test_fetch_report_blank_200_raises_empty_body_error() -> None:
    client = AppsFlyerClient(token="t")
    spec = PullRequestSpec(report_type="geo_by_date_report")
    policy = RetryPolicy(sleep=lambda s: None)

    with requests_mock.Mocker() as m:
        m.get(_report_url(), status_code=200, text="   \n  ")
        with pytest.raises(EmptyBodyError):
            fetch_report(client, spec, "app1", _FROM, _TO, policy)


@pytest.mark.unit
def test_fetch_report_401_raises_auth_error_immediately() -> None:
    client = AppsFlyerClient(token="t")
    spec = PullRequestSpec(report_type="geo_by_date_report")
    calls = {"n": 0}
    policy = RetryPolicy(sleep=lambda s: calls.__setitem__("n", calls["n"] + 1))

    with requests_mock.Mocker() as m:
        m.get(_report_url(), status_code=401, text="unauthorized")
        with pytest.raises(AuthError):
            fetch_report(client, spec, "app1", _FROM, _TO, policy)

    assert calls["n"] == 0  # no retry/sleep for auth errors


@pytest.mark.unit
def test_fetch_report_400_raises_permanent_error_no_retry() -> None:
    client = AppsFlyerClient(token="t")
    spec = PullRequestSpec(report_type="geo_by_date_report")
    calls = {"n": 0}
    policy = RetryPolicy(sleep=lambda s: calls.__setitem__("n", calls["n"] + 1))

    with requests_mock.Mocker() as m:
        m.get(_report_url(), status_code=400, text="bad date range")
        with pytest.raises(PermanentError):
            fetch_report(client, spec, "app1", _FROM, _TO, policy)

    assert calls["n"] == 0
    assert len(m.request_history) == 1


@pytest.mark.unit
@pytest.mark.parametrize("status", [404, 416])
def test_fetch_report_spurious_404_416_is_retried_and_recovers(status: int) -> None:
    # Live backfill 2026-09-24: single chunks came back 416/404 and succeeded later.
    client = AppsFlyerClient(token="t")
    spec = PullRequestSpec(report_type="geo_by_date_report")
    policy = RetryPolicy(sleep=lambda s: None, max_retries=3)

    with requests_mock.Mocker() as m:
        m.get(
            _report_url(),
            [{"status_code": status, "text": "oops"}, {"status_code": 200, "text": "Date\n"}],
        )
        raw = fetch_report(client, spec, "app1", _FROM, _TO, policy)

    assert raw.status == 200
    assert raw.api_calls == 2


@pytest.mark.unit
def test_fetch_report_403_with_marker_is_rate_limited_and_honours_retry_after() -> None:
    client = AppsFlyerClient(token="t")
    spec = PullRequestSpec(report_type="geo_by_date_report")
    waits: list[float] = []
    policy = RetryPolicy(sleep=waits.append, max_retries=3, retry_jitter=0.0)

    with requests_mock.Mocker() as m:
        m.get(
            _report_url(),
            [
                {
                    "status_code": 403,
                    "text": "Limit reached for account",
                    "headers": {"Retry-After": "120"},
                },
                {"status_code": 200, "text": "Date\n2026-09-01\n"},
            ],
        )
        report = fetch_report(client, spec, "app1", _FROM, _TO, policy)

    assert report.status == 200
    assert waits == [120.0]  # max(retry_after=120, base=60) * attempt(1)


@pytest.mark.unit
def test_fetch_report_403_without_marker_retried_then_auth_error() -> None:
    client = AppsFlyerClient(token="t")
    spec = PullRequestSpec(report_type="geo_by_date_report")
    policy = RetryPolicy(sleep=lambda s: None, max_retries=2)

    with requests_mock.Mocker() as m:
        m.get(_report_url(), status_code=403, text="abuse protection triggered")
        with pytest.raises(AuthError):
            fetch_report(client, spec, "app1", _FROM, _TO, policy)

    assert len(m.request_history) == 2  # retried up to max_retries, then converted


@pytest.mark.unit
def test_fetch_report_429_is_rate_limited() -> None:
    client = AppsFlyerClient(token="t")
    spec = PullRequestSpec(report_type="geo_by_date_report")
    waits: list[float] = []
    policy = RetryPolicy(sleep=waits.append, max_retries=3)

    with requests_mock.Mocker() as m:
        m.get(
            _report_url(),
            [
                {"status_code": 429, "text": "too many requests"},
                {"status_code": 200, "text": "Date\n2026-09-01\n"},
            ],
        )
        report = fetch_report(client, spec, "app1", _FROM, _TO, policy)

    assert report.status == 200
    assert len(waits) == 1


@pytest.mark.unit
def test_fetch_report_5xx_is_transient_backoff() -> None:
    client = AppsFlyerClient(token="t")
    spec = PullRequestSpec(report_type="geo_by_date_report")
    waits: list[float] = []
    policy = RetryPolicy(
        sleep=waits.append, max_retries=3, transient_base_wait=1.0, retry_jitter=0.0
    )

    with requests_mock.Mocker() as m:
        m.get(
            _report_url(),
            [
                {"status_code": 503, "text": "service unavailable"},
                {"status_code": 200, "text": "Date\n2026-09-01\n"},
            ],
        )
        report = fetch_report(client, spec, "app1", _FROM, _TO, policy)

    assert report.status == 200
    assert waits == [1.0]


@pytest.mark.unit
def test_fetch_report_api_calls_counts_all_attempts() -> None:
    client = AppsFlyerClient(token="t")
    spec = PullRequestSpec(report_type="geo_by_date_report")
    policy = RetryPolicy(sleep=lambda s: None, max_retries=3, transient_base_wait=0.0)

    with requests_mock.Mocker() as m:
        m.get(
            _report_url(),
            [
                {"status_code": 503, "text": "unavailable"},
                {"status_code": 503, "text": "unavailable"},
                {"status_code": 200, "text": "Date\n2026-09-01\n"},
            ],
        )
        report = fetch_report(client, spec, "app1", _FROM, _TO, policy)

    assert report.api_calls == 3
