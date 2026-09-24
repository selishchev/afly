"""Exception hierarchy for the AppsFlyer HTTP client, and response classification.

Everything afly's retry/backoff logic (``afly.appsflyer.retry.RetryPolicy``)
and callers need to decide is captured in *which exception type* a call
raised — never a bare status-code check scattered across the codebase. This
module is the single place an HTTP response becomes one of these types, so
that decision is made once and made consistently.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

_LIMIT_MARKER = "limit reached for"


class AppsFlyerError(Exception):
    """Base for every error raised talking to the AppsFlyer API.

    ``body_excerpt`` is capped at 300 chars — enough to see the error
    message AppsFlyer sent without risking a multi-KB HTML error page ending
    up in a log line or a CLI's stderr.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: str = "",
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body_excerpt = (body or "")[:300]
        self.url = url


class AuthError(AppsFlyerError):
    """401, or a 403 that never turned into a real quota response after retries."""


class RateLimitError(AppsFlyerError):
    """403 with the ``"Limit reached for"`` marker, or a plain 429."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: str = "",
        url: str | None = None,
        retry_after: float | None = None,
        scope_hint: str | None = None,
    ) -> None:
        super().__init__(message, status=status, body=body, url=url)
        self.retry_after = retry_after
        self.scope_hint = scope_hint


class TransientError(AppsFlyerError):
    """5xx, a network/timeout failure, or a 403 without the quota marker.

    AppsFlyer sometimes fronts a real quota/abuse block with a bare 403 that
    carries no ``"Limit reached for"`` text — indistinguishable, at the HTTP
    layer, from a transient abuse-protection hiccup. Treating it as
    transient (retry with backoff) is the safe default; see
    ``RetryPolicy.execute`` for what happens once retries are exhausted.
    """


class PermanentError(AppsFlyerError):
    """Any other 4xx: bad app id (404), bad params/date-range (400), etc."""


class EmptyBodyError(AppsFlyerError):
    """A 200 response whose body has no CSV header line at all."""


def classify_response(status: int, body: str, headers: object) -> type[AppsFlyerError] | None:
    """Map an HTTP status + body to an error class, or ``None`` for success.

    ``headers`` isn't used to *classify* (only the status and, for 403, the
    body marker matter) — it's accepted here so callers can pass the same
    three values they'd pass to :func:`build_error` without re-shaping them.
    """
    if 200 <= status < 300:
        return None
    if status == 401:
        return AuthError
    if status == 403:
        return RateLimitError if _LIMIT_MARKER in body.lower() else TransientError
    if status == 429:
        return RateLimitError
    if 500 <= status < 600:
        return TransientError
    return PermanentError


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Parse a ``Retry-After`` header value into seconds from *now*.

    Accepts both forms RFC 7231 allows: an integer/float number of seconds,
    or an HTTP-date. Returns ``None`` for a missing/unparseable value, and
    never a negative number (a date already in the past clamps to 0).
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None

    try:
        return max(float(stripped), 0.0)
    except ValueError:
        pass

    try:
        target = parsedate_to_datetime(stripped)
    except (TypeError, ValueError, IndexError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)

    reference = now if now is not None else datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    return max((target - reference).total_seconds(), 0.0)


def _extract_scope_hint(body: str) -> str | None:
    """Best-effort snippet of *what* quota was hit, for logging/diagnostics.

    AppsFlyer's message shape after ``"Limit reached for "`` isn't
    documented/stable, so this is deliberately a short, best-effort excerpt
    rather than a parsed enum — good enough for a human reading a retry log,
    not meant to be branched on.
    """
    lowered = body.lower()
    idx = lowered.find(_LIMIT_MARKER)
    if idx == -1:
        return None
    tail = body[idx + len(_LIMIT_MARKER) : idx + len(_LIMIT_MARKER) + 120]
    for sep in (".", "\n", '"'):
        pos = tail.find(sep)
        if pos != -1:
            tail = tail[:pos]
    tail = tail.strip()
    return tail or None


def build_error(
    status: int,
    body: str,
    *,
    url: str | None = None,
    headers: object = None,
) -> AppsFlyerError:
    """Build the right :class:`AppsFlyerError` subclass instance for *status*/*body*.

    Raises :class:`ValueError` if *status* isn't actually an error status —
    callers should check :func:`classify_response` (or just call this only
    after confirming the response failed) rather than relying on this as the
    classification step itself.
    """
    headers = headers or {}
    error_cls = classify_response(status, body, headers)
    if error_cls is None:
        raise ValueError(f"status {status} is not an AppsFlyer error status")

    if error_cls is RateLimitError:
        retry_after_raw = _get_header(headers, "Retry-After")
        return RateLimitError(
            f"AppsFlyer rate limit hit (status {status})",
            status=status,
            body=body,
            url=url,
            retry_after=parse_retry_after(retry_after_raw),
            scope_hint=_extract_scope_hint(body),
        )

    messages = {
        AuthError: f"AppsFlyer authentication failed (status {status})",
        TransientError: f"AppsFlyer transient error (status {status})",
        PermanentError: f"AppsFlyer request rejected (status {status})",
    }
    return error_cls(messages[error_cls], status=status, body=body, url=url)


def _get_header(headers: object, name: str) -> str | None:
    """Case-insensitively fetch *name* from a mapping-like ``headers`` object."""
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    value = getter(name)
    if value is None:
        value = getter(name.lower())
    if value is None:
        value = getter(name.upper())
    return value
