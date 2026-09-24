"""Tests for `afly.database.loads.LoadsRepo` and `afly.database.locks.LocksRepo`."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from afly.database.loads import ChunkLoad, LoadsRepo
from afly.database.locks import LockHeldError, LocksRepo
from afly.utils.datetime_utils import now_utc
from tests.unit.fakes import FakeManager

_LOAD = ChunkLoad(
    run_id="run-1",
    extract="standard",
    app_id="app1",
    report_type="app_id_report",
    from_date=date(2026, 9, 1),
    to_date=date(2026, 9, 3),
    chunk_days=2,
    is_long=False,
)


# ── LoadsRepo ──────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_start_chunk_inserts_running_row() -> None:
    fake = FakeManager()
    repo = LoadsRepo(fake, "internal", "_afly_loads")
    started = datetime(2026, 9, 10, 3, 0, 0)

    repo.start_chunk(_LOAD, started)

    assert len(fake.inserted) == 1
    db, table, columns, rows = fake.inserted[0]
    assert (db, table) == ("internal", "_afly_loads")
    row = rows[0]
    assert row["run_id"] == "run-1"
    assert row["status"] == "running"
    assert row["rows"] == 0
    assert row["api_calls"] == 0
    assert row["started_at"] == started
    assert row["finished_at"] is None
    assert row["is_long"] == 0


@pytest.mark.unit
def test_finish_chunk_inserts_a_second_row_computes_duration() -> None:
    fake = FakeManager()
    repo = LoadsRepo(fake, "internal", "_afly_loads")
    started = datetime(2026, 9, 10, 3, 0, 0)
    finished = datetime(2026, 9, 10, 3, 0, 5, 500000)

    repo.finish_chunk(
        _LOAD, "success", started_at=started, finished_at=finished, rows=120, api_calls=1
    )

    assert len(fake.inserted) == 1
    row = fake.inserted[0][3][0]
    assert row["status"] == "success"
    assert row["rows"] == 120
    assert row["api_calls"] == 1
    assert row["duration_ms"] == 5500


@pytest.mark.unit
def test_finish_chunk_rejects_invalid_status() -> None:
    fake = FakeManager()
    repo = LoadsRepo(fake, "internal", "_afly_loads")

    with pytest.raises(ValueError, match="invalid status"):
        repo.finish_chunk(
            _LOAD, "bogus", started_at=datetime(2026, 9, 10), finished_at=datetime(2026, 9, 10)
        )


@pytest.mark.unit
def test_watermark_sql_shape() -> None:
    fake = FakeManager()
    fake.query_dicts_default = [{"wm": date(1970, 1, 1)}]
    repo = LoadsRepo(fake, "internal", "_afly_loads")

    repo.watermark("standard", "app1")

    sql, params = fake.query_dicts_calls[0]
    assert "max(to_date)" in sql
    assert "status = 'success'" in sql
    assert params == {"extract": "standard", "app_id": "app1"}


@pytest.mark.unit
def test_watermark_epoch_sentinel_becomes_none() -> None:
    fake = FakeManager()
    fake.query_dicts_default = [{"wm": date(1970, 1, 1)}]
    repo = LoadsRepo(fake, "internal", "_afly_loads")

    assert repo.watermark("standard", "app1") is None


@pytest.mark.unit
def test_watermark_real_date_passes_through() -> None:
    fake = FakeManager()
    fake.query_dicts_default = [{"wm": date(2026, 9, 10)}]
    repo = LoadsRepo(fake, "internal", "_afly_loads")

    assert repo.watermark("standard", "app1") == date(2026, 9, 10)


@pytest.mark.unit
def test_watermark_no_rows_is_none() -> None:
    fake = FakeManager()
    fake.query_dicts_default = []
    repo = LoadsRepo(fake, "internal", "_afly_loads")

    assert repo.watermark("standard", "app1") is None


@pytest.mark.unit
def test_long_calls_today_sums_non_running_long_calls() -> None:
    fake = FakeManager()
    fake.query_dicts_default = [{"app_id": "app1", "calls": 5}, {"app_id": "app2", "calls": 3}]
    repo = LoadsRepo(fake, "internal", "_afly_loads")

    total, per_app = repo.long_calls_today(date(2026, 9, 10))

    assert total == 8
    assert per_app == {"app1": 5, "app2": 3}
    sql, params = fake.query_dicts_calls[0]
    assert "is_long = 1" in sql
    assert "status != 'running'" in sql
    assert params == {"start": datetime(2026, 9, 10, 0, 0, 0)}


@pytest.mark.unit
def test_recent_passes_limit_param() -> None:
    fake = FakeManager()
    fake.query_dicts_default = [{"run_id": "run-1"}]
    repo = LoadsRepo(fake, "internal", "_afly_loads")

    rows = repo.recent(limit=10)

    assert rows == [{"run_id": "run-1"}]
    sql, params = fake.query_dicts_calls[0]
    assert "ORDER BY started_at DESC" in sql
    assert params == {"limit": 10}


# ── LocksRepo ──────────────────────────────────────────────────────────────


def _row(
    *, run_id: str, owner: str, started_at: datetime, timeout_seconds: int, status: str = "running"
) -> dict:
    return {
        "lock_key": "table:marts.af_reports",
        "run_id": run_id,
        "status": status,
        "owner": owner,
        "started_at": started_at,
        "updated_at": started_at,
        "timeout_seconds": timeout_seconds,
    }


@pytest.mark.unit
def test_acquire_raises_when_actively_held() -> None:
    fake = FakeManager()
    fake.query_dicts_queue = [
        [
            _row(
                run_id="other-run",
                owner="other-owner",
                started_at=now_utc() - timedelta(seconds=5),
                timeout_seconds=3600,
            )
        ]
    ]
    repo = LocksRepo(fake, "internal", "_afly_locks")

    with pytest.raises(LockHeldError) as excinfo:
        repo.acquire("table:marts.af_reports", "my-run", "me", 3600)

    assert excinfo.value.owner == "other-owner"
    assert excinfo.value.run_id == "other-run"
    # Never got to writing a row of our own.
    assert fake.inserted == []


@pytest.mark.unit
def test_acquire_overrides_stale_lock() -> None:
    fake = FakeManager()
    stale_started = now_utc() - timedelta(seconds=10)
    fake.query_dicts_queue = [
        [
            _row(
                run_id="other-run", owner="other-owner", started_at=stale_started, timeout_seconds=1
            )
        ],
        [_row(run_id="my-run", owner="me", started_at=now_utc(), timeout_seconds=3600)],
    ]
    repo = LocksRepo(fake, "internal", "_afly_locks")

    repo.acquire("table:marts.af_reports", "my-run", "me", 3600)  # must not raise

    assert len(fake.inserted) == 1
    assert fake.inserted[0][3][0]["run_id"] == "my-run"


@pytest.mark.unit
def test_acquire_force_skips_the_check_entirely() -> None:
    fake = FakeManager()
    # Only ONE queued response: the post-insert confirm read. If force
    # consumed a pre-check read too, this test would raise IndexError-ish
    # behaviour (falling back to query_dicts_default = []), which would in
    # turn make the confirm read see nothing and raise LockHeldError.
    fake.query_dicts_queue = [
        [_row(run_id="my-run", owner="me", started_at=now_utc(), timeout_seconds=3600)]
    ]
    repo = LocksRepo(fake, "internal", "_afly_locks")

    repo.acquire("table:marts.af_reports", "my-run", "me", 3600, force=True)  # must not raise

    assert len(fake.query_dicts_calls) == 1
    assert len(fake.inserted) == 1


@pytest.mark.unit
def test_acquire_lost_race_raises() -> None:
    fake = FakeManager()
    fake.query_dicts_queue = [
        [],  # pre-check: free
        [
            _row(run_id="someone-else", owner="them", started_at=now_utc(), timeout_seconds=3600)
        ],  # confirm: not us
    ]
    repo = LocksRepo(fake, "internal", "_afly_locks")

    with pytest.raises(LockHeldError) as excinfo:
        repo.acquire("table:marts.af_reports", "my-run", "me", 3600)

    assert excinfo.value.run_id == "someone-else"


@pytest.mark.unit
def test_release_preserves_owner_started_at_timeout() -> None:
    fake = FakeManager()
    started = now_utc() - timedelta(minutes=5)
    fake.query_dicts_queue = [
        [_row(run_id="my-run", owner="me", started_at=started, timeout_seconds=3600)]
    ]
    repo = LocksRepo(fake, "internal", "_afly_locks")

    repo.release("table:marts.af_reports", "my-run")

    row = fake.inserted[0][3][0]
    assert row["status"] == "released"
    assert row["owner"] == "me"
    assert row["started_at"] == started
    assert row["timeout_seconds"] == 3600


@pytest.mark.unit
def test_clear_releases_running_lock_and_returns_true() -> None:
    fake = FakeManager()
    fake.query_dicts_queue = [
        [_row(run_id="my-run", owner="me", started_at=now_utc(), timeout_seconds=1)]
    ]
    repo = LocksRepo(fake, "internal", "_afly_locks")

    # Note: timeout_seconds=1 — clear() must not care whether it's stale.
    cleared = repo.clear("table:marts.af_reports")

    assert cleared is True
    assert fake.inserted[0][3][0]["status"] == "released"


@pytest.mark.unit
def test_clear_returns_false_when_nothing_held() -> None:
    fake = FakeManager()
    fake.query_dicts_queue = [[]]
    repo = LocksRepo(fake, "internal", "_afly_locks")

    cleared = repo.clear("table:marts.af_reports")

    assert cleared is False
    assert fake.inserted == []


@pytest.mark.unit
def test_list_active_computes_age_and_stale() -> None:
    fake = FakeManager()
    old_started = now_utc() - timedelta(seconds=100)
    fake.query_dicts_default = [
        _row(run_id="r1", owner="me", started_at=old_started, timeout_seconds=10)
    ]
    repo = LocksRepo(fake, "internal", "_afly_locks")

    active = repo.list_active()

    assert len(active) == 1
    assert active[0]["stale"] is True
    assert active[0]["age_seconds"] >= 100
