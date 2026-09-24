"""``extracts/*.yml`` — one file = one AppsFlyer Pull API report configuration.

The shape here mirrors AppsFlyer's own report taxonomy (report_type/category/
media_source) plus afly's own pull-window and destination-table knobs. Most
fields are optional and fall back to ``ProjectConfig.defaults`` via
:meth:`ExtractConfig.with_defaults` — an extract only states what makes it
different from the project norm.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from afly.config import ConfigError
from afly.config.project_config import ExtractDefaults, QuotaConfig
from afly.schema import Granularity
from afly.utils.naming import qualify_table

# report_type values AppsFlyer's aggregate Pull API supports that afly does
# NOT yet load (see the class docstring gate below) — kept as a set so the
# rejection message and the Literal stay in sync if the roadmap grows.
_UNSUPPORTED_REPORT_TYPES = {"partners_report", "geo_report"}


class ExtractConfig(BaseModel):
    """One extract: what to pull from AppsFlyer and where it lands in ClickHouse.

    v1 deliberately supports only the **by-date** report family
    (``geo_by_date_report`` / ``partners_by_date_report`` / ``daily_report``):
    these carry a ``Date`` column, which is what afly's idempotent, date-keyed
    write (day-partition rebuild via REPLACE PARTITION) relies on. ``partners_report`` /
    ``geo_report`` have no ``Date`` column (they're totals-over-the-requested-
    range reports) and are rejected at validation time rather than silently
    mis-loaded — see the roadmap note in the validator below.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9_]+$")
    description: str | None = None
    enabled: bool = True
    report_type: Literal[
        "geo_by_date_report",
        "partners_by_date_report",
        "daily_report",
        "partners_report",
        "geo_report",
    ]
    category: Literal["standard", "facebook", "organic"] = "standard"
    media_source: str | None = None
    exclude_media_sources: list[str] = Field(default_factory=list)
    reattr: bool = False
    attribution_touch_type: Literal["click", "impression"] | None = None
    timezone: str | None = None
    # See ExtractDefaults.currency (afly.config.project_config) — AppsFlyer
    # ignores this for the aggregate geo reports afly pulls and always
    # returns the app's own currency; it does not make afly convert anything.
    currency: Literal["preferred", "USD"] | None = None
    apps: list[str] | None = None
    exclude_apps: list[str] = Field(default_factory=list)
    platforms: list[str] | None = None
    start_date: date | None = None
    lookback_days: int | None = Field(default=None, ge=0)
    include_current_day: bool | None = None
    chunk_days: int | None = Field(default=None, ge=1, le=90)
    table: str
    on_empty: Literal["skip", "replace"] | None = None
    keep_unknown_columns: bool | None = None
    # Override of ExtractDefaults.partition_granularity — see that field's
    # docstring. Two extracts writing the same `table:` must resolve to the
    # same value (checked by `granularity_conflicts_for` after with_defaults,
    # in afly.config.discovery.load_extracts) since a destination table has
    # exactly one PARTITION BY.
    partition_granularity: Granularity | None = None
    extra_params: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_semantics(self) -> ExtractConfig:
        if self.report_type in _UNSUPPORTED_REPORT_TYPES:
            raise ValueError(
                f"report_type '{self.report_type}' is not supported in this "
                "version (no Date column; see roadmap)"
            )
        if self.category == "organic" and self.media_source is not None:
            raise ValueError("category 'organic' cannot also set media_source")
        if self.media_source is not None and self.exclude_media_sources:
            raise ValueError("media_source and exclude_media_sources are mutually exclusive")
        forbidden = {"from", "to"} & self.extra_params.keys()
        if forbidden:
            raise ValueError(
                f"extra_params cannot set {', '.join(sorted(forbidden))} — "
                "the pull window is controlled by afly, not by extra_params"
            )
        # Fail fast on an unparsable `table:` at load time rather than at
        # write time deep into a run.
        qualify_table(self.table, "x")
        return self

    def with_defaults(self, defaults: ExtractDefaults) -> ExtractConfig:
        """Return a copy with every unset optional field filled from *defaults*.

        Pure — the caller's original config is untouched. Raises
        :class:`ConfigError` if ``start_date`` is still unset after the
        merge: unlike the other fields there is no safe built-in fallback
        for "which day does history start on".
        """
        merged = self.model_copy(
            update={
                "start_date": (
                    self.start_date if self.start_date is not None else defaults.start_date
                ),
                "lookback_days": (
                    self.lookback_days if self.lookback_days is not None else defaults.lookback_days
                ),
                "chunk_days": (
                    self.chunk_days if self.chunk_days is not None else defaults.chunk_days
                ),
                "include_current_day": (
                    self.include_current_day
                    if self.include_current_day is not None
                    else defaults.include_current_day
                ),
                "timezone": self.timezone if self.timezone is not None else defaults.timezone,
                "currency": self.currency if self.currency is not None else defaults.currency,
                "on_empty": self.on_empty if self.on_empty is not None else defaults.on_empty,
                "keep_unknown_columns": (
                    self.keep_unknown_columns
                    if self.keep_unknown_columns is not None
                    else defaults.keep_unknown_columns
                ),
                "partition_granularity": (
                    self.partition_granularity
                    if self.partition_granularity is not None
                    else defaults.partition_granularity
                ),
            }
        )
        if merged.start_date is None:
            raise ConfigError(
                f"extract '{self.name}': start_date is required (set it in the "
                "extract or in project defaults)"
            )
        return merged

    def resolved_table(self, default_db: str) -> tuple[str, str]:
        """``(database, table)`` for this extract's destination, per :func:`qualify_table`."""
        return qualify_table(self.table, default_db)

    @classmethod
    def from_yaml_file(cls, path: Path) -> ExtractConfig:
        """Load and validate one ``extracts/*.yml`` file."""
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


def _normalize_media_source(value: str) -> str:
    """Lowercase alphanumeric-only form used to compare media source names.

    AppsFlyer's own ``media_source`` values and the display names used in
    ``exclude_media_sources`` don't agree on casing or punctuation
    (``facebook`` vs ``Facebook Ads``, ``yandexdirect_int`` vs
    ``yandexdirectint``) — this is the shared normal form
    :func:`warnings_for` uses to detect "these two probably mean the same
    network" without hardcoding a synonym table.
    """
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _covers(excluded: str, media_source: str) -> bool:
    """True if a normalized ``excluded`` entry plausibly covers ``media_source``.

    Prefix matching in either direction, not exact equality — see
    :func:`_normalize_media_source` for why exact matching would miss the
    real-world pairs afly needs to catch (``facebook`` / ``facebookads``).
    """
    a, b = _normalize_media_source(excluded), _normalize_media_source(media_source)
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))


def granularity_conflicts_for(extracts: list[ExtractConfig]) -> list[str]:
    """Fatal (unlike :func:`warnings_for`) cross-extract check: same ``table:``, same granularity.

    A destination table has exactly one ``PARTITION BY`` expression — two
    extracts sharing a ``table:`` but resolving (after ``with_defaults``) to
    different ``partition_granularity`` values can't both be right, and
    letting the first extract to run win silently would make the second
    extract's rebuilds compute the wrong ``PARTITION BY ... = pid`` filter
    against a table it didn't create. Called from
    ``afly.config.discovery.load_extracts`` on every already-defaulted
    extract, so both ``afly validate`` and ``afly run`` refuse to proceed
    rather than writing against a mismatched table.

    Expects *extracts* to already have gone through ``with_defaults()`` (so
    ``partition_granularity`` is never ``None``) — the same precondition
    :func:`warnings_for` has for its other fields.
    """
    errors: list[str] = []
    by_table: dict[str, list[ExtractConfig]] = {}
    for extract in extracts:
        by_table.setdefault(extract.table, []).append(extract)

    for table, group in by_table.items():
        granularities = {e.partition_granularity for e in group}
        if len(granularities) > 1:
            detail = ", ".join(
                f"{e.name}={e.partition_granularity}" for e in sorted(group, key=lambda e: e.name)
            )
            errors.append(
                f"table '{table}': extracts disagree on partition_granularity ({detail}) — "
                "extracts sharing one destination table must resolve to the same granularity "
                "(set partition_granularity explicitly on each, or fix the project default)"
            )

    return errors


def warnings_for(extracts: list[ExtractConfig], quota: QuotaConfig) -> list[str]:
    """Non-fatal issues worth flagging across a set of loaded extracts.

    Two independent checks:

    - a ``chunk_days`` at or above ``quota.long_call_min_days`` moves an
      extract's calls into AppsFlyer's daily-budget tier, which is worth a
      heads-up since it's easy to set without realizing the quota
      consequence;
    - two extracts writing the **same table** where one is unfiltered
      (``media_source`` unset) and doesn't exclude a media source the other
      extract *does* filter on — a likely double-count, since both extracts
      would then pull overlapping rows into the same destination.
    """
    warnings: list[str] = []

    for extract in extracts:
        chunk_days = extract.chunk_days
        if chunk_days is not None and chunk_days >= quota.long_call_min_days:
            warnings.append(
                f"extract {extract.name}: chunk_days={chunk_days} counts against "
                "the daily long-call budgets"
            )

    by_table: dict[str, list[ExtractConfig]] = {}
    for extract in extracts:
        by_table.setdefault(extract.table, []).append(extract)

    for group in by_table.values():
        if len(group) < 2:
            continue
        unfiltered = [e for e in group if e.media_source is None]
        filtered = [e for e in group if e.media_source is not None]
        for base in unfiltered:
            for other in filtered:
                media_source = other.media_source
                assert media_source is not None
                already_covered = any(
                    _covers(excluded, media_source) for excluded in base.exclude_media_sources
                )
                if not already_covered:
                    warnings.append(
                        f"possible duplicate ownership of media source "
                        f"'{media_source}' between '{base.name}' and '{other.name}': "
                        f"add it to exclude_media_sources of '{base.name}'"
                    )

    return warnings
