"""``profiles.yml`` — AppsFlyer + ClickHouse credentials and alert channels.

Kept separate from ``afly_project.yml`` for the same reason dbt splits the
two: credentials are per-machine/per-environment and env-interpolated, while
the project config is committed as-is. See ``afly.utils.env_interpolation``
for the ``${VAR}`` / ``{{ env_var('VAR') }}`` syntaxes this file resolves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from afly.config import ConfigError
from afly.utils.env_interpolation import find_unresolved, interpolate_env_vars


class AppsFlyerProfile(BaseModel):
    """AppsFlyer Pull API credentials for one profile."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1)
    base_url: str = "https://hq1.appsflyer.com"
    timeout_seconds: int = Field(default=120, ge=1)
    user_agent: str | None = None

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")


class ClickHouseProfile(BaseModel):
    """ClickHouse connection for one profile — native (``clickhouse-driver``) or HTTP (``clickhouse-connect``).

    ``protocol`` picks the transport; ``ClickHouseManager.from_profile``
    (``afly.database.clickhouse``) is the only place that reads it to decide
    which client to build. Both protocols speak to the same warehouse — which
    one a given deployment needs is an operational fact (native blocked by a
    firewall, only HTTP reachable, or vice versa), not a data-shape
    difference, so nothing else in ``afly.database`` branches on it.
    """

    model_config = ConfigDict(extra="forbid")

    host: str
    protocol: Literal["native", "http"] = "native"
    # `None` means "use the protocol's own default port" — see `effective_port`.
    # Distinct from a hardcoded default so a profile that doesn't set `port`
    # follows `protocol` (native 9000/9440, http 8123/8443) instead of always
    # landing on the native port even when `protocol: http` is set. A profile
    # written before HTTP support existed and pinning `port: 9000` keeps
    # working unchanged.
    port: int | None = None
    user: str = "default"
    password: str = ""
    database: str
    # Separate database for afly's own `_afly_loads`/`_afly_locks` bookkeeping
    # tables — unset means "same as `database`" (the common case: one schema
    # holds both the destination tables and afly's internal state).
    internal_database: str | None = None
    # Database for the transient `<table>__afly_staging` tables of the
    # partition rebuild — unset means "the destination's own database". Set it
    # when the destination database is mirrored automatically (e.g. a
    # Distributed-wrapper sync over `raw`), so staging tables never get
    # published and never leave dangling wrappers behind.
    staging_database: str | None = None
    secure: bool = False
    verify: bool = True
    settings: dict[str, Any] = Field(default_factory=dict)
    connect_timeout: int = Field(default=10, ge=1)
    send_receive_timeout: int = Field(default=600, ge=1)

    @property
    def internal_db(self) -> str:
        """The database afly's own tables live in — ``internal_database`` if set, else ``database``."""
        return self.internal_database or self.database

    @property
    def effective_port(self) -> int:
        """The port to actually connect on: ``port`` if set, else ``protocol``'s default.

        Native: 9000 plain, 9440 with ``secure``. HTTP: 8123 plain, 8443 with
        ``secure`` — the same defaults ``clickhouse-connect`` itself would
        pick, made explicit here so :func:`afly.database.checks.run_clickhouse_checks`
        can report the exact port used without reaching into the client.
        """
        if self.port is not None:
            return self.port
        if self.protocol == "native":
            return 9440 if self.secure else 9000
        return 8443 if self.secure else 8123


class AlertChannelConfig(BaseModel):
    """A named destination for run-failure alerts (see ``ErrorAlertingConfig``)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["mattermost", "slack", "webhook"]
    # Empty is allowed on purpose: a channel declared in profiles.yml but with
    # its env var unset (alerting off, or a laptop without the webhook secret)
    # must not block every command. Sending to it is skipped with a warning.
    webhook_url: str = ""
    channel: str | None = None
    username: str = "afly"
    icon_emoji: str | None = None
    timeout: int = Field(default=10, ge=1)
    # A link to the orchestrator's run for this invocation (e.g. a Prefect
    # flow-run URL), typically written with env placeholders so it resolves
    # per-invocation — see docs/guides/alerting.md. Unlike webhook_url, an
    # unresolved/empty value is never an error and never a warning: a laptop
    # run genuinely has no orchestrator, so afly.alerting.webhook silently
    # omits the link from the payload rather than nagging about it.
    run_url: str = ""


class ProfileConfig(BaseModel):
    """One named profile: one AppsFlyer account + one ClickHouse destination."""

    model_config = ConfigDict(extra="forbid")

    appsflyer: AppsFlyerProfile
    clickhouse: ClickHouseProfile


class ProfilesConfig(BaseModel):
    """Top-level ``profiles.yml`` model: every named profile + alert channels."""

    model_config = ConfigDict(extra="forbid")

    default_profile: str | None = None
    profiles: dict[str, ProfileConfig] = Field(min_length=1)
    alert_channels: dict[str, AlertChannelConfig] = Field(default_factory=dict)

    # Populated by `from_yaml_file(..., strict_env=False)` with the env-var
    # names that were still unresolved after interpolation. Left empty by
    # ordinary construction (`model_validate`, `strict_env=True`) and
    # excluded from `model_dump()` — it's a load-time diagnostic, not part
    # of the schema `profiles.yml` itself declares.
    unresolved_env: list[str] = Field(default_factory=list, exclude=True)

    @model_validator(mode="after")
    def _default_profile_exists(self) -> ProfilesConfig:
        if self.default_profile is not None and self.default_profile not in self.profiles:
            available = ", ".join(sorted(self.profiles)) or "(none defined)"
            raise ValueError(
                f"default_profile '{self.default_profile}' is not defined in "
                f"profiles — available: {available}"
            )
        return self

    def get_profile(self, name: str | None = None) -> tuple[str, ProfileConfig]:
        """Resolve *name* (or ``default_profile``) to ``(name, ProfileConfig)``.

        Raises :class:`ConfigError` listing the available profile names —
        every CLI command that takes ``--profile`` funnels through this so
        "unknown profile" and "no profile given, no default set" read the
        same way everywhere.
        """
        resolved = name or self.default_profile
        if resolved is None:
            available = ", ".join(sorted(self.profiles)) or "(none defined)"
            raise ConfigError(
                "no --profile given and no default_profile set in afly_project.yml — "
                f"available profiles: {available}"
            )
        if resolved not in self.profiles:
            available = ", ".join(sorted(self.profiles)) or "(none defined)"
            raise ConfigError(f"unknown profile '{resolved}' — available: {available}")
        return resolved, self.profiles[resolved]

    @classmethod
    def from_yaml_file(cls, path: Path, *, strict_env: bool = True) -> ProfilesConfig:
        """Load ``profiles.yml``, interpolating env vars before validation.

        With ``strict_env=True`` (the default, used by every command that
        actually connects to AppsFlyer/ClickHouse) a placeholder left
        unresolved in a credential field is a hard :class:`ConfigError` —
        better to fail at load time naming the missing env var than to send
        a literal ``${CLICKHOUSE_PASSWORD}`` string as a password. With
        ``strict_env=False`` (used by ``afly validate``, which must succeed
        even in an environment with no secrets exported) the same
        unresolved names are collected into ``.unresolved_env`` instead of
        raising, so the caller can render them as warnings.
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

        interpolated = interpolate_env_vars(raw)
        unresolved = sorted(set(_credential_placeholder_names(interpolated)))

        if strict_env and unresolved:
            raise ConfigError(f"{path}: unresolved environment variables: {', '.join(unresolved)}")

        try:
            config = cls.model_validate(interpolated)
        except ValidationError as exc:
            raise ConfigError(f"{path}: {exc}") from exc

        config.unresolved_env = unresolved
        return config


def _credential_placeholder_names(data: dict[str, Any]) -> list[str]:
    """Collect unresolved env-var names from the fields that matter for auth.

    Scoped deliberately to the credential-bearing fields (rather than every
    string in the file) — a stray literal ``${FOO}`` in, say, a profile name
    the user typed on purpose is not afly's business; a silently-empty
    token or password is.
    """
    names: list[str] = []
    for profile in (data.get("profiles") or {}).values():
        if not isinstance(profile, dict):
            continue
        appsflyer = profile.get("appsflyer") or {}
        if isinstance(appsflyer, dict) and "token" in appsflyer:
            names.extend(find_unresolved(appsflyer["token"]))
        clickhouse = profile.get("clickhouse") or {}
        if isinstance(clickhouse, dict):
            for field in ("password", "host", "user"):
                if field in clickhouse:
                    names.extend(find_unresolved(clickhouse[field]))
    # alert_channels.*.webhook_url is deliberately NOT here: an unset webhook
    # only disables that channel (skipped with a warning at send time), it
    # must not stop a run that has nothing to alert about.
    return names
