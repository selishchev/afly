"""Failure-alert delivery over a webhook (Mattermost/Slack "attachments", or plain JSON).

This only ever fires for a *run-level* failure (the pipeline itself broke —
auth, ClickHouse, an aborted run) — see
``afly.config.project_config.ErrorAlertingConfig`` and
``afly.run.runner._send_alerts``. It is not a data-quality alert.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

import requests

from afly import __version__
from afly.config.profile import AlertChannelConfig

logger = logging.getLogger(__name__)

_ATTACHMENT_COLOR = "#D63232"


def send_failure_alert(
    channel: AlertChannelConfig,
    *,
    project: str,
    profile: str,
    run_id: str,
    title: str,
    lines: Sequence[str],
    mentions: Sequence[str] = (),
    post: Callable[..., Any] = requests.post,
) -> bool:
    """POST a failure alert to *channel*. Never raises — returns ``False`` and
    logs a warning on any failure (network error, non-2xx, bad channel config).

    The webhook URL (which may itself embed a token, per Mattermost/Slack
    convention) is only ever used as the POST target — nothing here echoes
    any credential back into the message body.
    """
    try:
        payload = _build_payload(
            channel, project=project, run_id=run_id, title=title, lines=lines, mentions=mentions
        )
        response = post(channel.webhook_url, json=payload, timeout=channel.timeout)
        response.raise_for_status()
        return True
    except Exception:
        logger.warning(
            "failed to send failure alert via %s channel for run %s",
            channel.type,
            run_id,
            exc_info=True,
        )
        return False


def _build_payload(
    channel: AlertChannelConfig,
    *,
    project: str,
    run_id: str,
    title: str,
    lines: Sequence[str],
    mentions: Sequence[str],
) -> dict[str, Any]:
    if channel.type == "webhook":
        return {"title": title, "text": "\n".join(lines), "project": project, "run_id": run_id}

    # mattermost / slack: both speak the same incoming-webhook "attachments" shape.
    payload: dict[str, Any] = {
        "username": channel.username,
        "text": " ".join(mentions),
        "attachments": [
            {
                "color": _ATTACHMENT_COLOR,
                "title": title,
                "text": "\n".join(lines),
                "footer": f"afly {__version__}",
            }
        ],
    }
    if channel.icon_emoji:
        payload["icon_emoji"] = channel.icon_emoji
    if channel.channel:
        payload["channel"] = channel.channel
    return payload


__all__ = ["send_failure_alert"]
