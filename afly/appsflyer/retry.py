"""Retry/backoff policy for AppsFlyer API calls.

Centralizes the "how many times, how long to wait" decision so
``pull_api.fetch_report`` and ``mng_api.list_apps`` don't each hand-roll their
own backoff loop — and so M4's quota scheduler can observe rate-limit hits
(``on_rate_limit``) without the call sites themselves knowing it's watching.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

from afly.appsflyer.errors import AppsFlyerError, AuthError, RateLimitError, TransientError
from afly.utils.retry_defaults import (
    RETRY_JITTER,
    TRANSIENT_BASE_WAIT_SECONDS,
    TRANSIENT_MAX_WAIT_SECONDS,
)

T = TypeVar("T")


@dataclass
class RetryPolicy:
    """Executes a callable, retrying on :class:`RateLimitError`/:class:`TransientError`.

    - ``RateLimitError`` waits ``max(retry_after, rate_limit_base_wait) * attempt``
      (so successive hits back off linearly: 60s, 120s, 180s, ... at the
      defaults) before retrying — unless ``defer_rate_limits`` is set (see
      below).
    - ``TransientError`` waits an exponential ``transient_base_wait * 2**(n-1)``,
      capped at ``transient_cap`` — 30s/60s/120s/240s at the defaults before
      ``max_retries`` (5) runs out.
    - Both waits above are then **jittered**: ``actual = nominal * (1 +
      retry_jitter * U)``, ``U`` uniform in ``[0, 1)`` via ``rand()``. This
      only ever lengthens a wait (never shortens it, so a rate-limit wait
      never drops below AppsFlyer's own per-minute spacing) — the point is to
      stop every sibling key/process from waking up and hitting AppsFlyer
      again at exactly the same instant. ``rand`` is injectable (defaults to
      :func:`random.random`) so tests can pin it to ``0`` (minimum wait) or
      close to ``1`` (near-maximum) instead of tolerating flakiness.
    - Every other :class:`AppsFlyerError` (``AuthError``, ``PermanentError``,
      ``EmptyBodyError``) propagates immediately — retrying a bad token or a
      404 can't ever succeed.
    - After ``max_retries`` attempts, the last error is re-raised — *except*
      a 403-without-the-quota-marker ``TransientError`` (see
      ``errors.TransientError``'s docstring), which is re-raised as
      ``AuthError`` instead: if retrying never turns a bare 403 into a 200,
      a bad/expired token is the more likely explanation than an
      indefinitely-ongoing abuse-protection block, and callers should stop
      treating it as "try again later".

    ``defer_rate_limits`` exists for callers that have somewhere better than
    ``time.sleep`` to put a rate-limited job — namely ``afly run``'s
    ``QuotaScheduler``, which can hand out *other* keys while this one cools
    down instead of blocking the whole process. When set, a
    ``RateLimitError`` is re-raised on the very first hit — no sleep, no
    retry loop — after ``on_rate_limit`` (if any) still runs once, so the
    caller can react immediately. Defaults to ``False`` so ``apps``/``debug``
    and other direct callers keep the original inline-sleep behaviour.

    ``defer_transient`` is the same idea applied to ``TransientError``: a
    single-threaded executor sleeping through a 5xx/network backoff (up to
    ~7.5 minutes at the defaults, see ``transient_base_wait``/
    ``transient_cap``) stalls every other key in the run just as badly as an
    un-deferred rate limit does — a production run once stood still 13+
    minutes on one chunk this way. When set, a ``TransientError`` is
    re-raised on the very first hit — no sleep, no retry loop, no 403→
    ``AuthError`` escalation (that decision needs the *accumulated* attempt
    count across re-dispatches, which only the caller's scheduler tracks —
    see ``afly.run.scheduler.QuotaScheduler.defer_transient``). Defaults to
    ``False`` so ``apps``/``debug`` and app-list resolution keep retrying
    inline — they have no per-key scheduler to defer into.
    """

    max_retries: int = 5
    rate_limit_base_wait: float = 60.0
    transient_base_wait: float = TRANSIENT_BASE_WAIT_SECONDS
    transient_cap: float = TRANSIENT_MAX_WAIT_SECONDS
    retry_jitter: float = RETRY_JITTER
    sleep: Callable[[float], None] = field(default=time.sleep)
    rand: Callable[[], float] = field(default=random.random)
    on_rate_limit: Callable[[RateLimitError, int], None] | None = None
    defer_rate_limits: bool = False
    defer_transient: bool = False
    last_attempts: int = field(default=0, init=False)

    def _jittered(self, nominal: float) -> float:
        """Apply the ``retry_jitter`` spread — see the class docstring."""
        return nominal * (1 + self.retry_jitter * self.rand())

    def execute(self, fn: Callable[[], T]) -> T:
        """Call *fn* until it succeeds or retries are exhausted."""
        attempt = 0
        last_error: AppsFlyerError | None = None

        while attempt < self.max_retries:
            attempt += 1
            self.last_attempts = attempt
            try:
                return fn()
            except RateLimitError as exc:
                last_error = exc
                if self.on_rate_limit is not None:
                    self.on_rate_limit(exc, attempt)
                if self.defer_rate_limits:
                    raise
                if attempt >= self.max_retries:
                    break
                wait = self._jittered(
                    max(exc.retry_after or 0.0, self.rate_limit_base_wait) * attempt
                )
                self.sleep(wait)
            except TransientError as exc:
                last_error = exc
                if self.defer_transient:
                    raise
                if attempt >= self.max_retries:
                    if exc.status == 403:
                        raise AuthError(
                            str(exc), status=exc.status, body=exc.body_excerpt, url=exc.url
                        ) from exc
                    break
                nominal = min(self.transient_base_wait * (2 ** (attempt - 1)), self.transient_cap)
                self.sleep(self._jittered(nominal))

        assert last_error is not None  # loop always sets it before exiting via break
        raise last_error
