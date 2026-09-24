"""AppsFlyer management API — the account's app list.

Used by ``afly apps`` (a standalone lookup) and by ``afly debug``/``afly run``
to validate an app id before spending a Pull API call on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from afly.appsflyer.client import AppsFlyerClient
from afly.appsflyer.errors import build_error, classify_response
from afly.appsflyer.retry import RetryPolicy

_APPS_PATH = "/api/mng/apps"


@dataclass(frozen=True)
class AppInfo:
    """One app from the AppsFlyer account, as much as afly needs of it."""

    id: str
    name: str
    platform: str
    currency: str | None
    time_zone: str | None


def list_apps(client: AppsFlyerClient, policy: RetryPolicy, limit: int = 1000) -> list[AppInfo]:
    """Fetch every app visible to *client*'s token, paginating by offset.

    Stops once a page returns fewer than *limit* rows, or once ``offset``
    reaches ``meta.total_items`` — whichever comes first, so a server that
    omits ``total_items`` (or gets it wrong) still terminates correctly off
    the short-page signal alone.
    """
    apps: list[AppInfo] = []
    offset = 0
    total_items: int | None = None

    while True:
        page = _fetch_page(client, policy, limit, offset)
        data = page.get("data") or []
        apps.extend(_to_app_info(item) for item in data)

        meta = page.get("meta") or {}
        if total_items is None:
            total_items = meta.get("total_items")

        offset += limit
        if len(data) < limit:
            break
        if total_items is not None and offset >= total_items:
            break

    apps.sort(key=lambda a: a.id)
    return apps


def _fetch_page(
    client: AppsFlyerClient, policy: RetryPolicy, limit: int, offset: int
) -> dict[str, Any]:
    def _attempt() -> dict[str, Any]:
        response = client.get(
            _APPS_PATH, params={"limit": limit, "offset": offset}, accept="application/json"
        )
        error_cls = classify_response(response.status_code, response.text, response.headers)
        if error_cls is not None:
            raise build_error(
                response.status_code, response.text, url=response.url, headers=response.headers
            )
        result: dict[str, Any] = response.json()
        return result

    return policy.execute(_attempt)


def _to_app_info(item: dict[str, Any]) -> AppInfo:
    attrs = item.get("attributes") or {}
    return AppInfo(
        id=item.get("id", ""),
        name=attrs.get("name", ""),
        platform=attrs.get("platform", ""),
        currency=attrs.get("currency"),
        time_zone=attrs.get("time_zone"),
    )


def filter_apps(apps: list[AppInfo], platforms: list[str] | None) -> list[AppInfo]:
    """Keep only apps whose platform matches one of *platforms* (case-insensitive).

    ``platforms=None`` (or empty) means "no filter" — returns a copy of *apps*.
    """
    if not platforms:
        return list(apps)
    wanted = {p.lower() for p in platforms}
    return [a for a in apps if (a.platform or "").lower() in wanted]
