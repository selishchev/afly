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
from afly.utils.env_interpolation import find_unresolved

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


def _resolve_run_url(channel: AlertChannelConfig) -> str | None:
    """The channel's run link, or ``None`` if it's empty or still unresolved.

    ``run_url`` is typically written with env placeholders (a Prefect flow-run
    URL assembled from ``${PREFECT_UI_BASE_URL}``/``${PREFECT__FLOW_RUN_ID}`,
    say) that only resolve inside an actual orchestrator run — a laptop run
    has no orchestrator at all. Either case (never set, or set but the env
    vars aren't there this time) is silently "no link", never a config error
    or a warning — see ``AlertChannelConfig.run_url``'s docstring.
    """
    if not channel.run_url or find_unresolved(channel.run_url):
        return None
    return channel.run_url


def _normalize_mention(mention: str) -> str:
    """``oncall`` / ``@oncall`` / ``@@oncall`` all become exactly ``@oncall``."""
    return "@" + mention.lstrip("@")


def _attachment_text(lines: Sequence[str], run_url: str | None, mentions: Sequence[str]) -> str:
    """The attachment body: the message *lines*, then — separated by one blank
    line — the run link (if any) just above the mentions (if any)."""
    tail = [line for line in (run_url,) if line]
    if mentions:
        tail.append(" ".join(_normalize_mention(m) for m in mentions))
    body = "\n".join(lines)
    return body if not tail else f"{body}\n\n" + "\n".join(tail)


def _build_payload(
    channel: AlertChannelConfig,
    *,
    project: str,
    run_id: str,
    title: str,
    lines: Sequence[str],
    mentions: Sequence[str],
) -> dict[str, Any]:
    run_url = _resolve_run_url(channel)

    if channel.type == "webhook":
        return {
            "title": title,
            "text": "\n".join(lines),
            "project": project,
            "run_id": run_id,
            "run_url": run_url,
            "mentions": [_normalize_mention(m) for m in mentions],
        }

    # mattermost / slack: both speak the same incoming-webhook "attachments"
    # shape. Mentions (and the run link) live INSIDE the attachment text, not
    # the top-level `text` — Mattermost/Slack render the top-level `text`
    # above the colored attachment bar, which read as a stray, unstyled first
    # line once the attachment itself also carried content; folding everything
    # into one block reads as a single message.
    payload: dict[str, Any] = {
        "username": channel.username,
        "text": "",
        "attachments": [
            {
                "color": _ATTACHMENT_COLOR,
                "title": title,
                "text": _attachment_text(lines, run_url, mentions),
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
