"""``afly_project.yml`` — the project-wide config: paths, table names, extract
defaults, AppsFlyer quota budget, and error alerting.

Every model here uses ``extra="forbid"``: a typo'd key (``chunck_days``) must
fail loudly at config-load time, not get silently ignored and leave the user
wondering why their setting had no effect.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal, TypedDict

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from afly.config import ConfigError
from afly.schema import DEFAULT_PARTITION_GRANULARITY, Granularity
from afly.utils.retry_defaults import (
    RETRY_JITTER,
    TRANSIENT_BASE_WAIT_SECONDS,
    TRANSIENT_MAX_WAIT_SECONDS,
)


class PathsConfig(BaseModel):
    """Where project-relative directories live."""

    model_config = ConfigDict(extra="forbid")

    extracts: str = "extracts"


class TablesConfig(BaseModel):
    """Names of afly's own bookkeeping tables (idempotency ledger + locks)."""

    model_config = ConfigDict(extra="forbid")

    loads: str = "_afly_loads"
    locks: str = "_afly_locks"


class RetryPolicyKwargs(TypedDict):
    """Shape of :meth:`QuotaConfig.retry_policy_kwargs` — a plain ``dict[str,
    float]`` return type makes mypy treat every one of ``RetryPolicy``'s other
    (non-float) keyword-only fields as a possible target of a ``**`` call-site
    unpacking, which it then rejects. Naming the exact three keys here is what
    lets ``RetryPolicy(..., **quota.retry_policy_kwargs())`` type-check.
    """

    transient_base_wait: float
    transient_cap: float
    retry_jitter: float


class ExtractDefaults(BaseModel):
    """Project-wide fallback values for the optional fields of an extract.

    An extract only needs to override what makes it different from the norm
    (see ``ExtractConfig.with_defaults``) — most projects share one
    ``lookback_days``/``chunk_days``/``on_empty`` policy across every report.
    """

    model_config = ConfigDict(extra="forbid")

    start_date: date | None = None
    lookback_days: int = Field(default=3, ge=0)
    chunk_days: int = Field(default=2, ge=1, le=90)
    include_current_day: bool = True
    timezone: str | None = None
    # Still a real AppsFlyer Pull API query param (see PullRequestSpec) — but
    # AppsFlyer ignores currency=USD for the aggregate geo reports afly pulls
    # and always returns each app's own currency instead (verified live
    # 2026-09-23). afly stores whatever currency the response actually came
    # in as the destination `currency` column (afly/schema.py) — this field
    # does NOT convert the numbers; convert downstream if you need one
    # currency across apps.
    currency: Literal["preferred", "USD"] = "preferred"
    on_empty: Literal["skip", "replace"] = "skip"
    keep_unknown_columns: bool = False
    # Project-wide app ids to drop from EVERY extract's app list, on top of
    # whatever that extract's own `exclude_apps:` already names — see
    # ExtractConfig.with_defaults, which UNIONS this into the extract's list
    # rather than falling back to it. Use this for an app the whole project
    # should never pull (e.g. a decommissioned/test app id), instead of
    # repeating it in every extract's own `exclude_apps:`.
    exclude_apps: list[str] = Field(default_factory=list)
    # "month" (default) -> PARTITION BY toYYYYMM(date); "day" -> toYYYYMMDD(date).
    # See afly.schema.Granularity / afly.database.ddl.destination_ddl. A real
    # destination table has been measured at ~1.7MB/41k rows per 23 days, so
    # daily partitions mean hundreds of tiny parts a year — ClickHouse's own
    # guidance is coarse (monthly) partitions, hence the default. Extracts
    # sharing one `table:` must agree on this (afly.config.extract_config.
    # granularity_conflicts_for) since a destination table has exactly one
    # PARTITION BY.
    partition_granularity: Granularity = DEFAULT_PARTITION_GRANULARITY


class QuotaConfig(BaseModel):
    """AppsFlyer Pull API rate-limit budget.

    Mirrors AppsFlyer's own two-tier limiter: "short" calls (date ranges up
    to a couple of days) are throttled per-minute and don't count against a
    daily budget; "long" calls (wider ranges) draw down a per-account and
    per-app daily allowance. These numbers are *budgets afly enforces on
    itself*, not values read from AppsFlyer — set them to match your
    account's actual contract.

    ``max_waves_in_flight`` controls the executor's cross-wave lookahead: how
    many plan waves (see ``afly.run.planner.Plan.waves``) the scheduler may
    hand out jobs from at once. ``1`` reproduces the old strict
    one-wave-at-a-time behaviour — a wave whose only remaining jobs are
    rate-limited stalls the whole run even though every other key is idle.
    The default, ``8``, lets the executor keep dispatching ready jobs from
    several further-ahead waves while a deferred job in an earlier one cools
    down; a wave is still rebuilt only once *every* one of its own jobs is
    terminal, and strictly in wave order. (Raised from an initial ``2`` after
    a live backfill still spent 59 of 95 minutes idle with only two waves
    admitted — see ``docs/guides/quotas.md#wave-lookahead``.)

    ``transient_base_wait_seconds``/``transient_max_wait_seconds``/
    ``retry_jitter`` configure :class:`afly.appsflyer.retry.RetryPolicy`'s
    backoff for ``TransientError`` (5xx/network/an un-marked 403) and, via
    ``retry_jitter`` only, the linear ``RateLimitError`` backoff too (inline
    *and* ``QuotaScheduler``'s deferral — see that module). The nominal
    transient wait is ``min(transient_base_wait_seconds * 2**(attempt-1),
    transient_max_wait_seconds)`` — 30s/60s/120s/240s at the defaults before
    `max_retries` (5) is exhausted. ``retry_jitter`` then multiplies *every*
    computed wait (transient or rate-limit) by ``1 + retry_jitter * U`` (``U``
    uniform in ``[0, 1)``) — it only ever lengthens a wait, so a rate-limit
    wait can never drop below AppsFlyer's own per-minute spacing; the point is
    to stop every sibling key from waking up and hitting AppsFlyer again at
    exactly the same moment.
    """

    model_config = ConfigDict(extra="forbid")

    short_call_interval_seconds: int = Field(default=65, ge=0)
    long_call_min_days: int = Field(default=3, ge=1)
    account_long_calls_per_day: int = Field(default=120, ge=0)
    app_long_calls_per_day: int = Field(default=24, ge=0)
    reserve_long_calls: int = Field(default=0, ge=0)
    min_gap_seconds: float = Field(default=0.5, ge=0)
    max_retries: int = Field(default=5, ge=0)
    transient_base_wait_seconds: float = Field(default=TRANSIENT_BASE_WAIT_SECONDS, ge=0)
    transient_max_wait_seconds: float = Field(default=TRANSIENT_MAX_WAIT_SECONDS, ge=0)
    retry_jitter: float = Field(default=RETRY_JITTER, ge=0, le=1)
    max_waves_in_flight: int = Field(default=8, ge=1)

    def retry_policy_kwargs(self) -> RetryPolicyKwargs:
        """Keyword args for building a :class:`~afly.appsflyer.retry.RetryPolicy`
        from this project's retry/backoff tuning — the one place that mapping
        is spelled out, so every caller that builds a ``RetryPolicy`` from a
        loaded project (``afly run``/``afly apps``/``afly debug``) passes the
        same three fields the same way instead of re-deriving them.
        """
        return {
            "transient_base_wait": self.transient_base_wait_seconds,
            "transient_cap": self.transient_max_wait_seconds,
            "retry_jitter": self.retry_jitter,
        }


class ErrorAlertingConfig(BaseModel):
    """Where to send a "the run itself failed" alert (not a data-quality one)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    channels: list[str] = Field(default_factory=list)
    mentions: list[str] = Field(default_factory=list)


class ProjectConfig(BaseModel):
    """Top-level ``afly_project.yml`` model.

    Example:
        ```yaml
        name: my_project
        version: "1.0"
        default_profile: prod
        defaults:
          lookback_days: 3
          chunk_days: 2
        ```
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    version: str = "1.0"
    default_profile: str
    paths: PathsConfig = Field(default_factory=PathsConfig)
    tables: TablesConfig = Field(default_factory=TablesConfig)
    defaults: ExtractDefaults = Field(default_factory=ExtractDefaults)
    quota: QuotaConfig = Field(default_factory=QuotaConfig)
    lock_timeout_seconds: int = Field(default=7200, ge=60, le=86400)
    error_alerting: ErrorAlertingConfig = Field(default_factory=ErrorAlertingConfig)

    @classmethod
    def from_yaml_file(cls, path: Path) -> ProjectConfig:
        """Load and validate ``afly_project.yml``.

        Raises :class:`ConfigError` — never a bare pydantic
        ``ValidationError`` — so every config-loading call site in the CLI
        can catch one exception type and print a path-qualified message.
        """
        if not path.exists():
            raise ConfigError(f"{path}: file not found")

        try:
            raw = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path}: invalid YAML — {exc}") from exc

        if not raw:
            raise ConfigError(f"{path}: file is empty")
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: expected a YAML mapping at the top level")

        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(f"{path}: {exc}") from exc
