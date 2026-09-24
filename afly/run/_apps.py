"""Resolve which AppsFlyer app ids each extract in a run should pull."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from afly.appsflyer.mng_api import AppInfo, filter_apps
from afly.config.discovery import LoadedExtract


@dataclass(frozen=True)
class AppsResolver:
    """The two closures :func:`build_apps_resolver` returns, bundled.

    Both close over the same memoized account-wide app list, so a run that
    needs one for real ends up paying for the mng-API call once, not twice.
    """

    apps_for: Callable[[LoadedExtract], list[str]]
    currency_for: Callable[[str], str | None]


def build_apps_resolver(list_all_apps: Callable[[], list[AppInfo]]) -> AppsResolver:
    """Return :class:`AppsResolver` closures for :func:`~afly.run.planner.build_plan`
    (``apps_for``) and for stamping ``ReportContext.currency`` (``currency_for``).

    ``list_all_apps`` is called at most once (memoized) and only if some
    extract actually needs the account's full app list (``apps: null`` in
    its config) — an extract that names its own ``apps:`` never triggers
    the AppsFlyer management-API call at all. Consequently ``currency_for``
    can legitimately return ``None`` for an app whose extract(s) all name
    explicit ``apps:`` — afly doesn't spend an extra AppsFlyer request just
    to learn a currency it can otherwise detect from the CSV headers
    themselves (see ``afly.csvmap.parser.ReportContext.currency``).
    """
    cache: list[AppInfo] | None = None
    currencies: dict[str, str] = {}

    def _all_apps() -> list[AppInfo]:
        nonlocal cache
        if cache is None:
            cache = list_all_apps()
            currencies.update({a.id: a.currency for a in cache if a.currency})
        return cache

    def apps_for(extract: LoadedExtract) -> list[str]:
        config = extract.config
        excluded = set(config.exclude_apps)
        if config.apps is not None:
            return [a for a in config.apps if a not in excluded]
        apps = filter_apps(_all_apps(), config.platforms)
        return [a.id for a in apps if a.id not in excluded]

    def currency_for(app_id: str) -> str | None:
        return currencies.get(app_id)

    return AppsResolver(apps_for=apps_for, currency_for=currency_for)


__all__ = ["AppsResolver", "build_apps_resolver"]
