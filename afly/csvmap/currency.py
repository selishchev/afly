"""Resolve the one ``currency`` value stamped onto every row of a parsed report.

Split out of ``afly.csvmap.parser`` to keep that module under the house
line-length norm — the currency-precedence logic below is also the kind of
thing worth unit-testing in isolation from CSV row parsing.
"""

from __future__ import annotations

from typing import Any

from afly.csvmap.headers import EVENT_KIND_SALES


def resolve_currency(ctx_currency: str | None, classified: dict[str, Any]) -> tuple[str, list[str]]:
    """Decide the report's ``currency`` from the app-list hint and the CSV headers.

    AppsFlyer reports each app's money columns (``Total Revenue``/``Total
    Cost``/``ARPU``/``Average eCPI``/the ``Sales in <CUR>`` event triples) in
    that app's own currency and ignores the API's `currency=USD` param for
    these aggregate reports (verified live 2026-09-23) — so the currency
    detected from the CSV's own ``Sales in <CUR>`` headers is the ground
    truth, and ``ctx_currency`` (the app-list API's stated currency, when the
    caller has it — see ``afly.csvmap.parser.ReportContext.currency``) is
    only a cross-check. Precedence: the header wins on a disagreement (with a
    warning); ``ctx_currency`` fills in when the header can't determine one
    at all (no sales headers, or more than one distinct currency among them
    — also warned, since that means the report mixes currencies and no
    single value can describe every row).
    """
    header_currencies = sorted(
        {
            c.currency
            for c in classified.values()
            if c.kind == "event" and c.name == EVENT_KIND_SALES and c.currency
        }
    )
    warnings: list[str] = []

    if len(header_currencies) > 1:
        warnings.append(
            "multiple sales currencies detected in CSV headers: "
            f"{', '.join(header_currencies)} — keeping all values in event_sales"
        )
        detected = None
    else:
        detected = header_currencies[0] if header_currencies else None

    if ctx_currency and detected and ctx_currency != detected:
        warnings.append(
            f"app currency ({ctx_currency}) disagrees with the currency detected from CSV "
            f"headers ({detected}) — using the header currency, since AppsFlyer reports "
            "money in the app's own currency regardless of the requested currency param"
        )
        return detected, warnings
    if ctx_currency:
        return ctx_currency, warnings
    return detected or "", warnings
