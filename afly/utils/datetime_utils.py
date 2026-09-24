"""Date/time helpers shared by config validation, selection windows, and run ids.

Contract: everywhere afly deals with "the current time" it means naive UTC —
AppsFlyer's Pull API reports are date-bucketed (no timezone in the response)
and ClickHouse's plain ``DateTime``/``Date`` columns are timezone-naive too,
so mixing in an aware datetime anywhere in the pipeline is a bug waiting to
surface as an off-by-some-hours row. Keep every internal timestamp naive UTC.
"""

from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta, timezone


def today_utc() -> date:
    """Return today's date in UTC (naive — AppsFlyer/ClickHouse convention)."""
    return datetime.now(timezone.utc).date()


def now_utc() -> datetime:
    """Return the current time as a naive UTC datetime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_date(value: str) -> date:
    """Parse an ISO ``YYYY-MM-DD`` string into a :class:`date`.

    Raises ``ValueError`` with a message naming the offending string and the
    expected format — CLI option parsing (``--from``/``--to``) surfaces this
    directly to the user, so a bare ``invalid literal for int()`` is not
    good enough.
    """
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"invalid date {value!r} — expected ISO format YYYY-MM-DD (e.g. 2026-09-01)"
        ) from exc


def new_run_id(now: datetime | None = None) -> str:
    """Build a sortable, collision-resistant run id.

    Shape: ``<UTC timestamp>-<6 hex chars>`` (e.g. ``20260922T101530Z-a1b2c3``).
    The timestamp component makes run ids sort chronologically and readable
    in logs/tables at a glance; the random suffix guards against two runs
    started in the same second (e.g. a retried process) colliding in the
    ``_afly_loads``/``_afly_locks`` tables, which key on run id.
    """
    moment = now if now is not None else now_utc()
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    suffix = secrets.token_hex(3)
    return f"{stamp}-{suffix}"


def daterange(start: date, end: date) -> list[date]:
    """Every date from *start* to *end*, inclusive, in order.

    Returns an empty list if ``start > end`` rather than raising — callers
    (chunking a pull window) can treat an empty/inverted window as "nothing
    to do" without a special case.
    """
    if start > end:
        return []
    days = (end - start).days
    return [start + timedelta(days=i) for i in range(days + 1)]
