"""AppsFlyer aggregate Pull API v5 — request building and report fetching.

Split into a pure request-builder (:func:`build_pull_request`, easy to unit
test param-by-param) and the actual network call (:func:`fetch_report`, which
drives it through a :class:`~afly.appsflyer.retry.RetryPolicy`) — so every
``category``/``reattr``/``extra_params`` edge case can be asserted on the
URL/params alone, without mocking HTTP for each one.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from afly.appsflyer.client import AppsFlyerClient
from afly.appsflyer.errors import EmptyBodyError, build_error, classify_response
from afly.appsflyer.retry import RetryPolicy

_RESERVED_PARAMS = ("from", "to")


@dataclass(frozen=True)
class PullRequestSpec:
    """What to pull — the AppsFlyer-facing knobs of one extract, decoupled
    from afly's own config model (see :meth:`from_extract`)."""

    report_type: str
    category: str = "standard"
    media_source: str | None = None
    reattr: bool = False
    attribution_touch_type: str | None = None
    timezone: str | None = None
    currency: str | None = None
    extra_params: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_extract(cls, extract: Any) -> PullRequestSpec:
        """Build a spec from an ``ExtractConfig``-shaped object, via ``getattr``.

        Reads by attribute name rather than importing ``afly.config``
        (owned by a different milestone) — keeps this module usable and
        testable independently of that package's exact type, both before it
        exists and after.
        """
        return cls(
            report_type=extract.report_type,
            category=getattr(extract, "category", "standard") or "standard",
            media_source=getattr(extract, "media_source", None),
            reattr=bool(getattr(extract, "reattr", False)),
            attribution_touch_type=getattr(extract, "attribution_touch_type", None),
            timezone=getattr(extract, "timezone", None),
            currency=getattr(extract, "currency", None),
            extra_params=dict(getattr(extract, "extra_params", None) or {}),
        )


def build_pull_request(
    spec: PullRequestSpec,
    app_id: str,
    from_date: date,
    to_date: date,
    base_url: str,
) -> tuple[str, dict[str, str]]:
    """Build the ``(path, params)`` for one Pull API v5 call.

    ``base_url`` is accepted (not used in the returned path, which is always
    the same ``/api/agg-data/export/...`` shape regardless of region) purely
    so callers can pass everything they know about the target in one place
    without the function silently depending on a client's private state.
    """
    path = f"/api/agg-data/export/app/{app_id}/{spec.report_type}/v5"
    params: dict[str, str] = {"from": from_date.isoformat(), "to": to_date.isoformat()}

    if spec.media_source:
        params["media_source"] = spec.media_source
    if spec.category and spec.category != "standard":
        params["category"] = spec.category
    if spec.reattr:
        params["reattr"] = "true"
    if spec.attribution_touch_type == "impression":
        params["attribution_touch_type"] = "impression"
    if spec.timezone:
        params["timezone"] = spec.timezone
    if spec.currency and spec.currency != "preferred":
        params["currency"] = spec.currency

    for key, value in spec.extra_params.items():
        if key in _RESERVED_PARAMS:
            raise ValueError(
                f"extra_params may not override {key!r} — it's controlled by the pull window"
            )
        params[key] = value

    return path, params


@dataclass
class RawReport:
    """The raw fetched CSV plus enough metadata to log/debug the call."""

    text: str
    status: int
    url: str
    params: dict[str, str]
    api_calls: int
    elapsed_ms: int


def fetch_report(
    client: AppsFlyerClient,
    spec: PullRequestSpec,
    app_id: str,
    from_date: date,
    to_date: date,
    policy: RetryPolicy,
) -> RawReport:
    """Fetch one report, retrying per *policy*. Returns the decoded CSV body.

    ``api_calls``/``elapsed_ms`` on the result cover the *whole* fetch
    (every retried attempt), not just the final successful one — that's what
    a quota scheduler (M4) or a debug summary actually wants to know: what
    did this fetch cost, not what did its last attempt cost.
    """
    path, params = build_pull_request(spec, app_id, from_date, to_date, client.base_url)
    start_calls = client.api_calls
    start_time = time.monotonic()

    def _attempt() -> tuple[str, int, str]:
        response = client.get(path, params=params, accept="text/csv")
        error_cls = classify_response(response.status_code, response.text, response.headers)
        if error_cls is not None:
            raise build_error(
                response.status_code, response.text, url=response.url, headers=response.headers
            )

        text = response.content.decode("utf-8-sig")
        if not text.strip():
            raise EmptyBodyError(
                "AppsFlyer returned an empty report body",
                status=response.status_code,
                body="",
                url=response.url,
            )
        return text, response.status_code, response.url

    text, status, url = policy.execute(_attempt)

    return RawReport(
        text=text,
        status=status,
        url=url,
        params=dict(params),
        api_calls=client.api_calls - start_calls,
        elapsed_ms=int((time.monotonic() - start_time) * 1000),
    )
