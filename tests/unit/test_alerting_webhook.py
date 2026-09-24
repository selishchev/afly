"""Unit tests for afly.alerting.webhook.send_failure_alert — payload shape + never-raises."""

from __future__ import annotations

from typing import Any

import pytest

from afly.alerting.webhook import send_failure_alert
from afly.config.profile import AlertChannelConfig

_SECRET_URL = "https://hooks.example.com/services/T0/B0/super-secret-token-should-not-leak"  # pragma: allowlist secret


class _FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


def _channel(**overrides: object) -> AlertChannelConfig:
    base: dict[str, object] = {
        "type": "mattermost",
        "webhook_url": _SECRET_URL,
        "username": "afly",
        "timeout": 10,
    }
    base.update(overrides)
    return AlertChannelConfig(**base)  # type: ignore[arg-type]


def _recording_post(calls: list[dict[str, Any]], *, status: int = 200):
    def _post(url: str, json: dict[str, Any], timeout: int) -> _FakeResponse:
        calls.append({"url": url, "json": json, "timeout": timeout})
        return _FakeResponse(status)

    return _post


@pytest.mark.unit
def test_mattermost_payload_shape() -> None:
    calls: list[dict[str, Any]] = []
    channel = _channel(type="mattermost", channel="ops", icon_emoji=":rotating_light:")

    ok = send_failure_alert(
        channel,
        project="demo",
        profile="prod",
        run_id="run-1",
        title="afly run failed",
        lines=["line one", "line two"],
        mentions=["@oncall"],
        post=_recording_post(calls),
    )

    assert ok is True
    assert len(calls) == 1
    payload = calls[0]["json"]
    assert payload["username"] == "afly"
    assert payload["channel"] == "ops"
    assert payload["icon_emoji"] == ":rotating_light:"
    assert payload["text"] == "@oncall"
    attachment = payload["attachments"][0]
    assert attachment["title"] == "afly run failed"
    assert attachment["text"] == "line one\nline two"
    assert attachment["color"] == "#D63232"
    assert "afly" in attachment["footer"]
    assert calls[0]["url"] == _SECRET_URL
    assert calls[0]["timeout"] == 10


@pytest.mark.unit
def test_slack_payload_shares_attachments_shape() -> None:
    calls: list[dict[str, Any]] = []
    channel = _channel(type="slack", channel=None, icon_emoji=None)

    send_failure_alert(
        channel,
        project="demo",
        profile="prod",
        run_id="run-1",
        title="t",
        lines=["a"],
        post=_recording_post(calls),
    )

    payload = calls[0]["json"]
    assert "attachments" in payload
    assert "channel" not in payload  # omitted when unset
    assert "icon_emoji" not in payload


@pytest.mark.unit
def test_webhook_type_is_plain_json() -> None:
    calls: list[dict[str, Any]] = []
    channel = _channel(type="webhook")

    send_failure_alert(
        channel,
        project="demo",
        profile="prod",
        run_id="run-42",
        title="the title",
        lines=["l1", "l2"],
        post=_recording_post(calls),
    )

    payload = calls[0]["json"]
    assert payload == {
        "title": "the title",
        "text": "l1\nl2",
        "project": "demo",
        "run_id": "run-42",
    }


@pytest.mark.unit
def test_returns_false_on_http_error_and_never_raises() -> None:
    calls: list[dict[str, Any]] = []
    channel = _channel()

    ok = send_failure_alert(
        channel,
        project="demo",
        profile="prod",
        run_id="run-1",
        title="t",
        lines=["a"],
        post=_recording_post(calls, status=500),
    )

    assert ok is False


@pytest.mark.unit
def test_returns_false_and_never_raises_on_network_exception() -> None:
    def _boom(*args: object, **kwargs: object) -> Any:
        raise ConnectionError("no route to host")

    channel = _channel()
    ok = send_failure_alert(
        channel, project="demo", profile="prod", run_id="run-1", title="t", lines=["a"], post=_boom
    )
    assert ok is False


@pytest.mark.unit
def test_token_never_appears_in_payload() -> None:
    calls: list[dict[str, Any]] = []
    channel = _channel(type="mattermost")

    send_failure_alert(
        channel,
        project="demo",
        profile="prod",
        run_id="run-1",
        title="t",
        lines=["a", "b"],
        post=_recording_post(calls),
    )

    payload_str = str(calls[0]["json"])
    assert "super-secret-token-should-not-leak" not in payload_str
