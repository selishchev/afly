"""RunDeps — every external seam ``run_pipeline`` uses, with real defaults.

Split out of ``runner.py`` to keep that module under the house line-length
norm. Every default factory lazily imports the ClickHouse layer
(``afly.database.*``) inside its own body — that module is owned by a
concurrently-developed milestone — so importing this module never requires
it to exist; only actually *calling* a default (i.e. running for real,
without test-supplied overrides) does.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from afly.appsflyer.client import AppsFlyerClient
from afly.appsflyer.mng_api import AppInfo, list_apps
from afly.cli._project import ProjectContext, load_context
from afly.config.profile import ProfileConfig
from afly.schema import Granularity
from afly.utils.datetime_utils import now_utc, today_utc


def _default_manager_factory(profile: ProfileConfig) -> Any:
    from afly.database.clickhouse import ClickHouseManager

    return ClickHouseManager.from_profile(profile.clickhouse)


def _default_client_factory(profile: ProfileConfig) -> AppsFlyerClient:
    af = profile.appsflyer
    return AppsFlyerClient(
        token=af.token,
        base_url=af.base_url,
        timeout_seconds=af.timeout_seconds,
        user_agent=af.user_agent,
    )


def _default_alert_sender(channel: Any, **kwargs: Any) -> bool:
    from afly.alerting.webhook import send_failure_alert

    return send_failure_alert(channel, **kwargs)


def _default_loads_repo_factory(manager: Any, db: str, table: str) -> Any:
    from afly.database.loads import LoadsRepo

    return LoadsRepo(manager, db, table)


def _default_locks_repo_factory(manager: Any, db: str, table: str) -> Any:
    from afly.database.locks import LocksRepo

    return LocksRepo(manager, db, table)


def _default_rebuilder_factory(
    manager: Any, db: str, table: str, granularity: Granularity, *, staging_db: str | None = None
) -> Any:
    from afly.database.writer import PartitionRebuilder

    return PartitionRebuilder(manager, db, table, granularity, staging_db=staging_db)


def _default_ensure_internal_tables(
    manager: Any, internal_db: str, loads_table: str, locks_table: str
) -> None:
    from afly.database.tables import ensure_internal_tables

    ensure_internal_tables(manager, internal_db, loads_table, locks_table)


def _default_ensure_destination(
    manager: Any, db: str, table: str, granularity: Granularity
) -> bool:
    from afly.database.ddl import ensure_destination

    return ensure_destination(manager, db, table, granularity)


@dataclass
class RunDeps:
    """Factories/clocks ``run_pipeline`` uses — override for tests.

    The ``*_repo_factory``/``rebuilder_factory``/``ensure_*`` fields are the
    seam that lets a test substitute ``FakeLoadsRepo``/``FakeLocksRepo``/
    ``FakeRebuilder`` directly, without needing a full fake
    ``ClickHouseManager`` that implements every low-level method those real
    classes call — only ``manager_factory`` (and ``.close()`` on whatever it
    returns) needs a manager-shaped object at all.
    """

    load_context: Callable[..., ProjectContext] = load_context
    manager_factory: Callable[[ProfileConfig], Any] = _default_manager_factory
    client_factory: Callable[[ProfileConfig], AppsFlyerClient] = _default_client_factory
    list_apps: Callable[..., list[AppInfo]] = list_apps
    loads_repo_factory: Callable[[Any, str, str], Any] = _default_loads_repo_factory
    locks_repo_factory: Callable[[Any, str, str], Any] = _default_locks_repo_factory
    rebuilder_factory: Callable[..., Any] = _default_rebuilder_factory
    ensure_internal_tables: Callable[[Any, str, str, str], None] = _default_ensure_internal_tables
    ensure_destination: Callable[[Any, str, str, Granularity], bool] = _default_ensure_destination
    now: Callable[[], datetime] = now_utc
    today: Callable[[], date] = today_utc
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    alert_sender: Callable[..., bool] = field(default=_default_alert_sender)


__all__ = ["RunDeps"]
