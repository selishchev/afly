"""In-memory fakes for `afly run`'s unit tests.

Two dependency surfaces get faked here:

- the ClickHouse layer (``afly.database.*``) — ``FakeManager``,
  ``FakeLoadsRepo``, ``FakeLocksRepo``, ``FakeRebuilder`` stand in for
  ``ClickHouseManager``/``LoadsRepo``/``LocksRepo``/``PartitionRebuilder``
  at the *repository* level (the executor/runner never touch a
  ``ClickHouseManager`` directly — see the interface contract in
  ``afly.run.executor``), so these tests never depend on that milestone
  landing, or on `clickhouse-driver` being reachable;
- AppsFlyer's HTTP layer — ``FakeAppsFlyer`` is a duck-typed stand-in for
  ``AppsFlyerClient`` (same ``.get()``/``.base_url``/``.api_calls``
  surface), so the *real* ``afly.appsflyer.pull_api.fetch_report`` +
  ``RetryPolicy`` + ``PullRequestSpec`` run unmodified against it — only the
  transport is faked, not the request-building/error-classification logic.

Also: ``FakeClock``/``FakeSleep`` for deterministic scheduler tests — calling
the fake ``sleep`` advances the fake clock instead of actually waiting.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from afly.config.discovery import LoadedExtract
from afly.config.extract_config import ExtractConfig

_PATH_RE = re.compile(r"^/api/agg-data/export/app/(?P<app_id>[^/]+)/(?P<report_type>[^/]+)/v5$")


def make_extract(name: str = "standard", **overrides: Any) -> ExtractConfig:
    """A fully-defaulted ``ExtractConfig`` for tests — every optional field
    that ``with_defaults()`` would normally fill in is pre-set here, so
    ``afly.run`` code that assumes a merged config (per-planner assertions)
    never sees an unmerged ``None``.
    """
    base: dict[str, Any] = {
        "name": name,
        "report_type": "geo_by_date_report",
        "table": f"appsflyer_{name}",
        "start_date": date(2026, 1, 1),
        "lookback_days": 3,
        "chunk_days": 2,
        "include_current_day": True,
        "on_empty": "skip",
        "keep_unknown_columns": False,
        "partition_granularity": "month",
    }
    base.update(overrides)
    return ExtractConfig.model_validate(base)


def make_loaded(name: str = "standard", path: str | None = None, **overrides: Any) -> LoadedExtract:
    """A :class:`LoadedExtract` wrapping :func:`make_extract`."""
    config = make_extract(name, **overrides)
    return LoadedExtract(path=Path(path or f"extracts/{name}.yml"), config=config)


# -- ClickHouse-layer fakes ------------------------------------------------


class FakeManager:
    """Stands in for ``ClickHouseManager`` — the runner only ever calls ``.close()``."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@dataclass(frozen=True)
class FakeChunkLoad:
    """Field-compatible stand-in for ``afly.database.loads.ChunkLoad``."""

    run_id: str
    extract: str
    app_id: str
    report_type: str
    from_date: date
    to_date: date
    chunk_days: int
    is_long: bool


@dataclass
class StartedChunk:
    load: Any
    started_at: datetime


@dataclass
class FinishedChunk:
    load: Any
    status: str
    started_at: datetime
    finished_at: datetime
    rows: int = 0
    api_calls: int = 0
    http_status: int | None = None
    error: str | None = None
    skip_reason: str | None = None


class FakeLoadsRepo:
    """Records ``start_chunk``/``finish_chunk`` calls; ``watermark``/``long_calls_today`` are scripted."""

    def __init__(
        self,
        *,
        watermarks: dict[tuple[str, str], date] | None = None,
        account_used: int = 0,
        app_used: dict[str, int] | None = None,
    ) -> None:
        self._watermarks = dict(watermarks or {})
        self._account_used = account_used
        self._app_used = dict(app_used or {})
        self.started: list[StartedChunk] = []
        self.finished: list[FinishedChunk] = []

    def start_chunk(self, load: Any, started_at: datetime) -> None:
        self.started.append(StartedChunk(load=load, started_at=started_at))

    def finish_chunk(
        self,
        load: Any,
        status: str,
        *,
        started_at: datetime,
        finished_at: datetime,
        rows: int = 0,
        api_calls: int = 0,
        http_status: int | None = None,
        error: str | None = None,
        skip_reason: str | None = None,
    ) -> None:
        self.finished.append(
            FinishedChunk(
                load=load,
                status=status,
                started_at=started_at,
                finished_at=finished_at,
                rows=rows,
                api_calls=api_calls,
                http_status=http_status,
                error=error,
                skip_reason=skip_reason,
            )
        )

    def watermark(self, extract: str, app_id: str) -> date | None:
        return self._watermarks.get((extract, app_id))

    def long_calls_today(self, today: date) -> tuple[int, dict[str, int]]:
        return self._account_used, dict(self._app_used)


@dataclass
class LockCall:
    lock_key: str
    run_id: str
    owner: str
    timeout_seconds: int
    force: bool


class LockHeldError(Exception):
    """Field-compatible stand-in for ``afly.database.locks.LockHeldError``."""

    def __init__(
        self, lock_key: str, owner: str, run_id: str, started_at: datetime, age_seconds: float
    ) -> None:
        super().__init__(f"lock {lock_key} held by {owner} (run {run_id})")
        self.lock_key = lock_key
        self.owner = owner
        self.run_id = run_id
        self.started_at = started_at
        self.age_seconds = age_seconds


class FakeLocksRepo:
    """Records ``acquire``/``release`` calls; can be scripted to raise on a given key."""

    def __init__(self, *, held: dict[str, LockHeldError] | None = None) -> None:
        self._held = dict(held or {})
        self.acquired: list[LockCall] = []
        self.released: list[tuple[str, str]] = []

    def acquire(
        self, lock_key: str, run_id: str, owner: str, timeout_seconds: int, *, force: bool = False
    ) -> None:
        if lock_key in self._held and not force:
            raise self._held[lock_key]
        self.acquired.append(LockCall(lock_key, run_id, owner, timeout_seconds, force))

    def release(self, lock_key: str, run_id: str) -> None:
        self.released.append((lock_key, run_id))


@dataclass
class RebuildCall:
    partition_id: str
    coverage: set[tuple[date, str, str]]
    fresh_rows: list[dict[str, Any]]

    @property
    def day(self) -> date:
        """Convenience for day-granularity tests: the single day this partition covers.

        Only meaningful when the ``FakeRebuilder`` that recorded this call
        used ``granularity="day"`` (its default) — a ``"month"`` partition id
        (``"202609"``) doesn't round-trip through this.
        """
        return datetime.strptime(self.partition_id, "%Y%m%d").date()


@dataclass
class FakeRebuildResult:
    partition_id: str
    kept_rows: int
    fresh_rows: int
    action: str


class FakeRebuilder:
    """Records every ``prepare_staging``/``drop_staging``/``rows_for_pair``/``rebuild_partition`` call.

    Defaults to **day** granularity — unlike the production default
    (``month``, see ``afly.schema.DEFAULT_PARTITION_GRANULARITY``) — because
    most executor/runner tests build dates a day apart and assert one
    rebuild call per day; day granularity keeps that 1:1 mapping so those
    tests read the same as before configurable granularity landed. Pass
    ``granularity="month"`` for a test that specifically exercises
    cross-day partition grouping (see ``tests/unit/test_rebuild.py``, which
    calls ``afly.run._rebuild.rebuild_wave`` directly).

    ``existing_rows`` scripts ``rows_for_pair()``'s return value per
    ``(day, extract, app_id)``; ``rebuild_action`` scripts the action
    ``rebuild_partition()`` reports back for every call (default
    ``"replace"``).
    """

    def __init__(
        self,
        *,
        granularity: str = "day",
        existing_rows: dict[tuple[date, str, str], int] | None = None,
        rebuild_action: str = "replace",
    ) -> None:
        self.granularity = granularity
        self._existing_rows = dict(existing_rows or {})
        self._rebuild_action = rebuild_action
        self.staging_prepared = False
        self.staging_dropped = False
        self.rebuild_calls: list[RebuildCall] = []
        self.rows_for_pair_calls: list[tuple[date, str, str]] = []

    def prepare_staging(self) -> None:
        self.staging_prepared = True

    def drop_staging(self) -> None:
        self.staging_dropped = True

    def rows_for_pair(self, day: date, extract: str, app_id: str) -> int:
        self.rows_for_pair_calls.append((day, extract, app_id))
        return self._existing_rows.get((day, extract, app_id), 0)

    def rebuild_partition(
        self,
        partition_id: str,
        coverage: set[tuple[date, str, str]],
        fresh_rows: list[dict[str, Any]],
    ) -> FakeRebuildResult:
        fresh_list = list(fresh_rows)
        self.rebuild_calls.append(
            RebuildCall(partition_id=partition_id, coverage=set(coverage), fresh_rows=fresh_list)
        )
        return FakeRebuildResult(
            partition_id=partition_id,
            kept_rows=0,
            fresh_rows=len(fresh_list),
            action=self._rebuild_action,
        )


class ExplodingRebuilder:
    """A rebuilder whose ``rebuild_partition`` always raises — for the ClickHouse-failure path."""

    def __init__(self, error: Exception | None = None, *, granularity: str = "day") -> None:
        self.error = error or RuntimeError("boom: staging swap failed")
        self.granularity = granularity
        self.staging_prepared = False
        self.staging_dropped = False

    def prepare_staging(self) -> None:
        self.staging_prepared = True

    def drop_staging(self) -> None:
        self.staging_dropped = True

    def rows_for_pair(self, day: date, extract: str, app_id: str) -> int:
        return 0

    def rebuild_partition(
        self, partition_id: str, coverage: set[tuple[date, str, str]], fresh_rows: list[Any]
    ) -> Any:
        raise self.error


# -- AppsFlyer HTTP-layer fake ----------------------------------------------


class _FakeResponse:
    def __init__(
        self, *, status_code: int, text: str, url: str, headers: dict[str, str] | None = None
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.url = url
        self.headers = headers or {}

    def json(self) -> Any:
        return json.loads(self.text)


class FakeAppsFlyer:
    """Duck-typed stand-in for ``AppsFlyerClient`` — pass directly as ``Executor(client=...)``.

    Script a response per ``(report_type, app_id, from_date, to_date)`` via
    :meth:`script`; ``.get()`` parses the real Pull-API URL shape, so the
    genuine ``afly.appsflyer.pull_api.fetch_report`` exercises this fake
    exactly as it would a real ``AppsFlyerClient`` — request building, retry,
    and error classification all stay real.

    A scripted *result* is one of:
    - a ``str`` — a 200 OK response with that CSV body (an empty/blank
      string triggers ``fetch_report``'s own ``EmptyBodyError``);
    - a ``(status_code, body)`` tuple — an HTTP-error response, classified
      by the real ``afly.appsflyer.errors.classify_response``;
    - a ``BaseException`` instance — raised directly from ``.get()`` (for
      simulating a raw network failure);
    - a ``list`` of any of the above — consumed front-to-back, one per call
      to the same ``(report_type, app_id, from_date, to_date)`` key, for
      scripting a job that fails N times (e.g. rate-limited) before it
      eventually succeeds. A single non-list value keeps repeating forever
      (the pre-existing behaviour), so every other call site is unaffected.
    """

    base_url = "https://fake.appsflyer.test"

    def __init__(self) -> None:
        self.api_calls = 0
        self._responses: dict[tuple[str, str, date, date], Any] = {}
        self.calls: list[tuple[str, str, date, date]] = []

    def script(
        self, report_type: str, app_id: str, from_date: date, to_date: date, result: Any
    ) -> None:
        self._responses[(report_type, app_id, from_date, to_date)] = result

    def reset_counter(self) -> None:
        self.api_calls = 0

    def get(
        self, path: str, params: dict[str, Any] | None = None, accept: str = "application/json"
    ) -> _FakeResponse:
        self.api_calls += 1
        params = params or {}
        match = _PATH_RE.match(path)
        assert match, f"FakeAppsFlyer: unrecognized path {path!r}"
        app_id = match.group("app_id")
        report_type = match.group("report_type")
        from_date = date.fromisoformat(params["from"])
        to_date = date.fromisoformat(params["to"])
        key = (report_type, app_id, from_date, to_date)
        self.calls.append(key)

        if key not in self._responses:
            raise AssertionError(f"FakeAppsFlyer: no scripted response for {key}")
        result = self._responses[key]
        if isinstance(result, list):
            if not result:
                raise AssertionError(f"FakeAppsFlyer: exhausted scripted responses for {key}")
            result = result.pop(0)

        if isinstance(result, BaseException):
            raise result
        if isinstance(result, tuple):
            status, body = result
            return _FakeResponse(status_code=status, text=body, url=f"{self.base_url}{path}")
        return _FakeResponse(status_code=200, text=result, url=f"{self.base_url}{path}")


# -- clock/sleep fakes -------------------------------------------------------


@dataclass
class FakeClock:
    time: float = 0.0

    def __call__(self) -> float:
        return self.time

    def advance(self, seconds: float) -> None:
        self.time += seconds


@dataclass
class FakeSleep:
    """A ``sleep()`` that advances a :class:`FakeClock` instead of actually waiting."""

    clock: FakeClock
    calls: list[float] = field(default_factory=list)

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)
