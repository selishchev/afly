"""Shared default numbers for AppsFlyer retry/backoff.

Both :class:`afly.config.project_config.QuotaConfig` (the user-facing
``quota:`` block) and :class:`afly.appsflyer.retry.RetryPolicy` (the thing
that actually sleeps) need the *same* fallback numbers — a project that never
sets ``quota:`` at all should behave identically to one that sets every field
to its documented default. Rather than hardcoding the numbers twice (and
inevitably drifting), both modules import them from here.

This lives in ``afly.utils`` rather than ``afly.config`` or
``afly.appsflyer`` because ``afly.appsflyer`` is documented (see the repo's
own ``CLAUDE.md`` module map) as taking **no config imports** — it only knows
about AppsFlyer's HTTP surface, not afly's YAML config shape. ``afly.utils``
is the lowest common layer both already depend on, so importing from here
creates no new layering violation in either direction.
"""

from __future__ import annotations

#: Nominal wait before the first transient-error retry (seconds). Doubles
#: each subsequent attempt (`transient_base_wait_seconds * 2**(attempt-1)`)
#: up to `TRANSIENT_MAX_WAIT_SECONDS`. 30s (not the old 2s) because a 5xx/
#: network blip from AppsFlyer has been observed to take longer than a couple
#: of seconds to clear — a short cap just burns through `max_retries` against
#: an outage that would have resolved itself given a few minutes.
TRANSIENT_BASE_WAIT_SECONDS = 30.0

#: Cap on the nominal (pre-jitter) transient backoff. At the defaults
#: (30s base, max_retries=5) the exponential sequence (30, 60, 120, 240) never
#: actually reaches this cap — it exists for callers with a higher
#: `max_retries`, or a project that lowers the base wait.
TRANSIENT_MAX_WAIT_SECONDS = 600.0

#: Fraction of extra, random spread added on top of every retry/deferral
#: wait: `actual = nominal * (1 + RETRY_JITTER * U)`, U uniform in [0, 1).
#: Always lengthens a wait, never shortens it — so a rate-limit wait can
#: never drop below AppsFlyer's own per-minute spacing. Spreads out
#: simultaneous retries across sibling keys/processes instead of having them
#: all wake up and hit AppsFlyer again at the exact same moment.
RETRY_JITTER = 0.25

__all__ = ["RETRY_JITTER", "TRANSIENT_BASE_WAIT_SECONDS", "TRANSIENT_MAX_WAIT_SECONDS"]
