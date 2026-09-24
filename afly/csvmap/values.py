"""Cell-level value coercion for AppsFlyer CSV reports.

AppsFlyer's CSV cells are all strings, with a handful of format quirks
(``N/A``/empty for null, ``%`` on rates, thousands separators on large
counts) that are the same regardless of which column they show up in — so
this module knows only about *shapes* (str/uint/float/date), never about
which destination column a value is headed for. That mapping is
``csvmap.headers``'s job.
"""

from __future__ import annotations

from datetime import date

NULL_TOKENS = frozenset({"", "N/A", "null", "None", "NULL", "-"})

# Dimensions keep AppsFlyer's literal "None": it is a real value, not a
# missing cell — AppsFlyer writes `Media Source (pid) = None` / `Campaign (c) =
# None` for traffic it could not attribute, and downstream models classify on
# it (e.g. "campaign = 'None' and no adset -> organic"). Folding it into ''
# would silently change that classification. Only truly empty markers blank a
# dimension; metrics still treat every NULL_TOKENS entry as null.
DIMENSION_NULL_TOKENS = frozenset({"", "N/A"})


def to_str(v: str | None) -> str:
    """A dimension value: trimmed, ``''`` only for an empty/``N/A`` cell."""
    if v is None:
        return ""
    stripped = v.strip()
    return "" if stripped in DIMENSION_NULL_TOKENS else stripped


def to_uint(v: str | None) -> int | None:
    """A non-negative integer metric, or ``None`` for null/negative/unparseable.

    Accepts thousands separators (``"12,000"``) and a trailing ``.0``
    (``"12.0"``, which AppsFlyer's exports do emit for integer-valued
    columns) by going through ``float`` first.
    """
    if v is None:
        return None
    stripped = v.strip()
    if stripped in NULL_TOKENS:
        return None
    try:
        value = float(stripped.replace(",", ""))
    except ValueError:
        return None
    if value < 0:
        return None
    return int(value)


def to_float(v: str | None) -> float | None:
    """A float metric (rate/currency), or ``None`` for null/unparseable.

    Strips ``%``/``$``/thousands separators before parsing — the caller
    decides what the resulting magnitude means (e.g. a ``%``-suffixed rate
    stays in "whole percent" units here; scaling to a 0..1 fraction, if
    wanted, is the caller's job, not this function's).
    """
    if v is None:
        return None
    stripped = v.strip()
    if stripped in NULL_TOKENS:
        return None
    cleaned = stripped.replace(",", "").replace("%", "").replace("$", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def to_date(v: str) -> date:
    """Parse an ISO ``YYYY-MM-DD`` cell. Raises :class:`ValueError` if unparseable."""
    stripped = v.strip()
    try:
        return date.fromisoformat(stripped)
    except ValueError as exc:
        raise ValueError(f"invalid date {v!r} — expected ISO format YYYY-MM-DD") from exc
