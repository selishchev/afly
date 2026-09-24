"""Unit tests for afly.run.planner: build_plan and render_plan."""

from __future__ import annotations

from datetime import date

import pytest

from afly.config.discovery import LoadedExtract
from afly.config.project_config import QuotaConfig
from afly.run.options import RunOptions
from afly.run.planner import ChunkJob, Plan, build_plan, render_plan
from afly.run.windows import Chunk

from .run_fakes import make_loaded

_TODAY = date(2026, 9, 22)
_QUOTA = QuotaConfig(
    long_call_min_days=3, account_long_calls_per_day=100, app_long_calls_per_day=20
)


def _options(**overrides: object) -> RunOptions:
    base: dict[str, object] = {"select": "*"}
    base.update(overrides)
    return RunOptions(**base)  # type: ignore[arg-type]


def _no_watermark(extract: str, app_id: str) -> date | None:
    return None


@pytest.mark.unit
def test_apps_intersection_with_options_apps() -> None:
    extract = make_loaded("standard")
    plan = build_plan(
        [extract],
        apps_for=lambda e: ["1", "2", "3"],
        watermark_for=_no_watermark,
        today=_TODAY,
        default_db="marts",
        quota=_QUOTA,
        options=_options(apps=["2", "3", "9"]),
    )
    ep = plan.extracts[0]
    assert ep.apps == ["2", "3"]  # "9" isn't in the extract's own app list


@pytest.mark.unit
def test_extract_level_exclude_apps_applied_before_options_apps() -> None:
    extract = make_loaded("standard", exclude_apps=["2"])
    plan = build_plan(
        [extract],
        apps_for=lambda e: [a for a in ["1", "2", "3"] if a not in set(e.config.exclude_apps)],
        watermark_for=_no_watermark,
        today=_TODAY,
        default_db="marts",
        quota=_QUOTA,
        options=_options(),
    )
    ep = plan.extracts[0]
    assert ep.apps == ["1", "3"]


@pytest.mark.unit
def test_no_apps_selected_produces_noop_note() -> None:
    extract = make_loaded("standard")
    plan = build_plan(
        [extract],
        apps_for=lambda e: [],
        watermark_for=_no_watermark,
        today=_TODAY,
        default_db="marts",
        quota=_QUOTA,
        options=_options(),
    )
    ep = plan.extracts[0]
    assert ep.jobs == []
    assert ep.note == "nothing to do (no apps selected)"


@pytest.mark.unit
def test_chunk_days_override_applies_to_every_job() -> None:
    extract = make_loaded("standard", chunk_days=2, start_date=date(2026, 1, 1))
    plan = build_plan(
        [extract],
        apps_for=lambda e: ["1"],
        watermark_for=_no_watermark,
        today=date(2026, 1, 10),
        default_db="marts",
        quota=_QUOTA,
        options=_options(chunk_days=5),
    )
    for job in plan.jobs:
        assert job.chunk.days <= 5


@pytest.mark.unit
def test_chunk_days_falls_back_to_extract_config_when_not_overridden() -> None:
    extract = make_loaded("standard", chunk_days=2, start_date=date(2026, 1, 1))
    plan = build_plan(
        [extract],
        apps_for=lambda e: ["1"],
        watermark_for=_no_watermark,
        today=date(2026, 1, 10),
        default_db="marts",
        quota=_QUOTA,
        options=_options(),
    )
    assert all(job.chunk.days <= 2 for job in plan.jobs)


@pytest.mark.unit
def test_waves_group_by_chunk_index_across_extracts() -> None:
    extract_a = make_loaded("standard", chunk_days=2, start_date=date(2026, 1, 1))
    extract_b = make_loaded("facebook", chunk_days=2, start_date=date(2026, 1, 1))
    plan = build_plan(
        [extract_a, extract_b],
        apps_for=lambda e: ["1"],
        watermark_for=_no_watermark,
        today=date(2026, 1, 6),
        default_db="marts",
        quota=_QUOTA,
        options=_options(),
    )
    waves = plan.waves()
    # Every wave is non-empty, ascending by index, and jobs across the two
    # extracts sharing a wave index actually land in the same wave.
    indices = [w[0].chunk.index for w in waves]
    assert indices == sorted(indices)
    for wave in waves:
        assert len({j.chunk.index for j in wave}) == 1
    # Both extracts contribute jobs somewhere.
    names_seen = {j.extract.config.name for wave in waves for j in wave}
    assert names_seen == {"standard", "facebook"}


@pytest.mark.unit
def test_is_long_uses_quota_long_call_min_days() -> None:
    extract = make_loaded("standard", chunk_days=5, start_date=date(2026, 1, 1))
    plan = build_plan(
        [extract],
        apps_for=lambda e: ["1"],
        watermark_for=_no_watermark,
        today=date(2026, 1, 10),
        default_db="marts",
        quota=_QUOTA,  # long_call_min_days=3
        options=_options(),
    )
    assert any(j.is_long for j in plan.jobs)
    assert all(j.is_long == (j.chunk.days >= 3) for j in plan.jobs)


@pytest.mark.unit
def test_plan_tables_and_long_jobs() -> None:
    extract = make_loaded("standard", table="marts.appsflyer_standard", chunk_days=5)
    plan = build_plan(
        [extract],
        apps_for=lambda e: ["1"],
        watermark_for=_no_watermark,
        today=date(2026, 1, 10),
        default_db="marts",
        quota=_QUOTA,
        options=_options(),
    )
    assert plan.tables() == {("marts", "appsflyer_standard")}
    assert plan.long_jobs() == [j for j in plan.jobs if j.is_long]


# -- render_plan --------------------------------------------------------


def _job(app_id: str, is_long: bool, extract: LoadedExtract | None = None) -> ChunkJob:
    extract = extract or make_loaded("standard")
    chunk = Chunk(index=0, from_date=date(2026, 1, 1), to_date=date(2026, 1, 3))
    return ChunkJob(
        extract=extract, app_id=app_id, chunk=chunk, is_long=is_long, db="marts", table="t"
    )


@pytest.mark.unit
def test_render_plan_flags_account_exceeds() -> None:
    quota = QuotaConfig(
        account_long_calls_per_day=2, app_long_calls_per_day=100, reserve_long_calls=0
    )
    jobs = [_job("1", True), _job("2", True), _job("3", True)]
    plan = Plan(extracts=[], jobs=jobs)
    lines = render_plan(plan, quota=quota, account_used=0, app_used={})
    assert any("EXCEEDS" in line and "account" in line for line in lines)


@pytest.mark.unit
def test_render_plan_flags_app_exceeds() -> None:
    quota = QuotaConfig(
        account_long_calls_per_day=100, app_long_calls_per_day=1, reserve_long_calls=0
    )
    jobs = [_job("app1", True), _job("app1", True)]
    plan = Plan(extracts=[], jobs=jobs)
    lines = render_plan(plan, quota=quota, account_used=0, app_used={})
    assert any("EXCEEDS" in line and "app1" in line for line in lines)


@pytest.mark.unit
def test_render_plan_no_exceeds_within_budget() -> None:
    quota = QuotaConfig(
        account_long_calls_per_day=100, app_long_calls_per_day=100, reserve_long_calls=0
    )
    jobs = [_job("app1", True)]
    plan = Plan(extracts=[], jobs=jobs)
    lines = render_plan(plan, quota=quota, account_used=0, app_used={})
    assert not any("EXCEEDS" in line for line in lines)


@pytest.mark.unit
def test_render_plan_seeded_usage_counts_toward_total() -> None:
    quota = QuotaConfig(
        account_long_calls_per_day=5, app_long_calls_per_day=100, reserve_long_calls=0
    )
    jobs = [_job("app1", True)]
    plan = Plan(extracts=[], jobs=jobs)
    lines = render_plan(plan, quota=quota, account_used=5, app_used={})
    assert any("EXCEEDS" in line and "account" in line for line in lines)


@pytest.mark.unit
def test_render_plan_wall_time_uses_total_jobs_floor_when_keys_are_shallow() -> None:
    """Many distinct, shallow keys: the busiest-key throttle bound is tiny,
    but the executor is serial, so total_jobs * 2s must win instead."""
    quota = QuotaConfig(short_call_interval_seconds=60)
    jobs = [_job(str(i), False) for i in range(100)]  # 100 distinct keys, depth 1 each
    plan = Plan(extracts=[], jobs=jobs)
    lines = render_plan(plan, quota=quota, account_used=0, app_used={})
    # busiest-key bound: 1 * 60s / 60 = 1.0 min; total-jobs bound: 100 * 2s / 60 ~= 3.3 min
    assert any("estimated wall time: 3.3 min" in line for line in lines)
    assert any("excluding retries" in line for line in lines)


@pytest.mark.unit
def test_render_plan_wall_time_uses_busiest_key_when_it_dominates() -> None:
    """One key, many jobs: the per-minute throttle on that single key
    dominates over the flat 2s/request floor."""
    quota = QuotaConfig(short_call_interval_seconds=60)
    extract = make_loaded("standard")
    jobs = [_job("app1", False, extract=extract) for _ in range(10)]  # 1 key, depth 10
    plan = Plan(extracts=[], jobs=jobs)
    lines = render_plan(plan, quota=quota, account_used=0, app_used={})
    # busiest-key bound: 10 * 60s / 60 = 10.0 min; total-jobs bound: 10 * 2s / 60 ~= 0.3 min
    assert any("estimated wall time: 10.0 min" in line for line in lines)
