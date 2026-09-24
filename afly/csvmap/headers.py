"""AppsFlyer CSV header → destination column mapping.

Two header shapes exist in the wild: a fixed set of dimension/metric column
headers (:data:`KNOWN_HEADERS`), and a per-in-app-event triple
(``"<event> (Unique users)"`` / ``"(Event counter)"`` / ``"(Sales in <CUR>)"``)
whose *event name* varies per report. The sales leg's currency varies too —
AppsFlyer reports each app's Sales figures in *that app's own* currency
(verified live 2026-09-23: `currency=USD` is ignored for these reports), so
``"(Sales in USD)"`` and ``"(Sales in EUR)"`` are the same triple *kind*, just
carrying a different currency. :func:`classify_header` tells a caller
(``csvmap.parser``) which shape one header is, so the parser never has to
duplicate this matching logic.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

# Standard-format headers (AppsFlyer's "standard" category) plus the
# Facebook-format additions (Campaign Name/Id, Adset/Adgroup Name/Id) — see
# the module docstring in afly.csvmap.parser for which report shapes carry
# which subset. Keys are matched after `.strip()`; classify_header() also
# offers a case-insensitive fallback for header casing drift.
KNOWN_HEADERS: dict[str, str] = {
    "Date": "date",
    "Country": "country",
    "Agency/PMD (af_prt)": "agency",
    "Media Source (pid)": "media_source",
    "Campaign (c)": "campaign",
    "Impressions": "impressions",
    "Clicks": "clicks",
    "CTR": "ctr",
    "Installs": "installs",
    "Conversion Rate": "conversion_rate",
    "Sessions": "sessions",
    "Loyal Users": "loyal_users",
    "Loyal Users/Installs": "loyal_users_rate",
    "Total Revenue": "total_revenue",
    "Total Cost": "total_cost",
    "ROI": "roi",
    "ARPU": "arpu",
    "Average eCPI": "average_ecpi",
    "Campaign Name": "campaign_name",
    "Campaign Id": "campaign_id",
    "Adset Name": "adset",
    "Adset Id": "adset_id",
    "Adgroup Name": "adgroup",
    "Adgroup Id": "adgroup_id",
}

# The three triple "kinds" a caller (afly.csvmap.parser) branches on. Sales
# is one kind regardless of which currency it was reported in — the currency
# itself rides along on ClassifiedHeader.currency instead of being baked into
# the kind, so a report mixing "Sales in EUR" and (hypothetically) "Sales in
# USD" headers is still one destination column (`event_sales`), not two.
EVENT_KIND_UNIQUE_USERS = "Unique users"
EVENT_KIND_EVENT_COUNTER = "Event counter"
EVENT_KIND_SALES = "Sales"

EVENT_TRIPLE_RE = re.compile(
    r"^(?P<event>.+) \((?P<kind>Unique users|Event counter|Sales in (?P<currency>[A-Z]{3}))\)$"
)
_EVENT_TRIPLE_RE_CI = re.compile(EVENT_TRIPLE_RE.pattern, re.IGNORECASE)

EVENT_KIND_TO_COLUMN: dict[str, str] = {
    EVENT_KIND_UNIQUE_USERS: "event_unique_users",
    EVENT_KIND_EVENT_COUNTER: "event_counter",
    EVENT_KIND_SALES: "event_sales",
}

_FACEBOOK_MARKER_HEADER = "Adset Id"


@dataclass(frozen=True)
class ClassifiedHeader:
    """What one CSV header means.

    ``kind`` is ``"column"`` (a fixed dimension/metric), ``"event"`` (one
    leg of an event triple), or ``"unknown"``.

    - ``kind == "column"``: ``name`` is the destination column name.
    - ``kind == "event"``: ``name`` is the triple *kind* (a key into
      :data:`EVENT_KIND_TO_COLUMN` — :data:`EVENT_KIND_SALES` for any
      ``"Sales in <CUR>"`` leg) and ``event_name`` is the event itself (e.g.
      ``"af_purchase"``). ``currency`` is set (the 3-letter code, upper-cased)
      only for the sales leg.
    - ``kind == "unknown"``: ``name``/``event_name``/``currency`` are all
      ``None``.

    Iterable as a 2-tuple (``kind, name = classify_header(h)``) for callers
    that only need the primary classification.
    """

    kind: str
    name: str | None
    event_name: str | None = None
    currency: str | None = None
    case_mismatch: bool = False

    def __iter__(self) -> Iterator[str | None]:
        return iter((self.kind, self.name))


def classify_header(header: str) -> ClassifiedHeader:
    """Classify one CSV header string (column / event triple / unknown)."""
    stripped = header.strip()

    if stripped in KNOWN_HEADERS:
        return ClassifiedHeader(kind="column", name=KNOWN_HEADERS[stripped])

    match = EVENT_TRIPLE_RE.match(stripped)
    if match:
        return _event_from_match(match, case_mismatch=False)

    # Case-insensitive fallback: header casing has drifted across AppsFlyer
    # report-format versions in the wild — treat a header that matches
    # apart from case as recognized-but-flagged rather than unknown.
    lowered = stripped.lower()
    for known, column in KNOWN_HEADERS.items():
        if known.lower() == lowered:
            return ClassifiedHeader(kind="column", name=column, case_mismatch=True)

    ci_match = _EVENT_TRIPLE_RE_CI.match(stripped)
    if ci_match:
        return _event_from_match(ci_match, case_mismatch=True)

    return ClassifiedHeader(kind="unknown", name=None)


def _event_from_match(match: re.Match[str], *, case_mismatch: bool) -> ClassifiedHeader:
    """Build the ``kind == "event"`` :class:`ClassifiedHeader` for a regex match.

    Shared by the exact and case-insensitive passes above — the only
    difference between them is which of the three literal kind-words matched
    and in what case, which this normalizes back to the canonical
    ``EVENT_KIND_*`` constant either way.
    """
    currency = match.group("currency")
    if currency is not None:
        return ClassifiedHeader(
            kind="event",
            name=EVENT_KIND_SALES,
            event_name=match.group("event"),
            currency=currency.upper(),
            case_mismatch=case_mismatch,
        )
    kind_text = match.group("kind")
    name = (
        EVENT_KIND_UNIQUE_USERS
        if kind_text.lower() == EVENT_KIND_UNIQUE_USERS.lower()
        else EVENT_KIND_EVENT_COUNTER
    )
    return ClassifiedHeader(
        kind="event", name=name, event_name=match.group("event"), case_mismatch=case_mismatch
    )


def detect_format(headers: list[str]) -> str:
    """``"facebook"`` if the Facebook-only ``Adset Id`` header is present, else ``"standard"``."""
    normalized = {h.strip() for h in headers}
    return "facebook" if _FACEBOOK_MARKER_HEADER in normalized else "standard"
