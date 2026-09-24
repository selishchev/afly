"""Unit tests for RetryPolicy — no real sleeping or HTTP involved.

``fn`` is a small stateful closure that raises a canned error for the first
N calls, then succeeds — the fake ``sleep`` records every wait so tests can
assert the exact backoff schedule without a slow test suite.
"""

from __future__ import annotations

import pytest

from afly.appsflyer.errors import (
    AuthError,
    EmptyBodyError,
    PermanentError,
    RateLimitError,
    TransientError,
)
from afly.appsflyer.retry import RetryPolicy


class _FakeSleep:
    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def _flaky(errors: list[Exception], result: str = "ok") -> object:
    """Return a 0-arg callable raising each of *errors* in turn, then *result*."""
    calls = {"n": 0}

    def _fn() -> str:
        i = calls["n"]
        calls["n"] += 1
        if i < len(errors):
            raise errors[i]
        return result

    return _fn


@pytest.mark.unit
def test_success_on_first_try_no_sleep() -> None:
    sleep = _FakeSleep()
    policy = RetryPolicy(sleep=sleep)
    assert policy.execute(lambda: "ok") == "ok"
    assert sleep.waits == []
    assert policy.last_attempts == 1


@pytest.mark.unit
def test_rate_limit_backoff_is_linear_in_attempt() -> None:
    sleep = _FakeSleep()
    policy = RetryPolicy(sleep=sleep, rate_limit_base_wait=60.0, max_retries=5)
    errors = [RateLimitError("hit", status=429, retry_after=None) for _ in range(3)]
    fn = _flaky(list(errors))

    assert policy.execute(fn) == "ok"  # type: ignore[arg-type]
    assert sleep.waits == [60.0, 120.0, 180.0]
    assert policy.last_attempts == 4


@pytest.mark.unit
def test_rate_limit_honours_retry_after_over_base_wait() -> None:
    sleep = _FakeSleep()
    policy = RetryPolicy(sleep=sleep, rate_limit_base_wait=60.0, max_retries=5)
    fn = _flaky([RateLimitError("hit", status=403, retry_after=250.0)])

    assert policy.execute(fn) == "ok"  # type: ignore[arg-type]
    # attempt 1: max(250, 60) * 1 = 250
    assert sleep.waits == [250.0]


@pytest.mark.unit
def test_rate_limit_retry_after_smaller_than_base_wait_uses_base() -> None:
    sleep = _FakeSleep()
    policy = RetryPolicy(sleep=sleep, rate_limit_base_wait=60.0, max_retries=5)
    fn = _flaky([RateLimitError("hit", status=429, retry_after=5.0)])

    assert policy.execute(fn) == "ok"  # type: ignore[arg-type]
    assert sleep.waits == [60.0]


@pytest.mark.unit
def test_on_rate_limit_hook_called_with_attempt_number() -> None:
    sleep = _FakeSleep()
    seen: list[tuple[int, int | None]] = []
    policy = RetryPolicy(
        sleep=sleep,
        max_retries=5,
        on_rate_limit=lambda exc, attempt: seen.append((attempt, exc.status)),
    )
    fn = _flaky([RateLimitError("hit", status=429), RateLimitError("hit", status=429)])

    assert policy.execute(fn) == "ok"  # type: ignore[arg-type]
    assert seen == [(1, 429), (2, 429)]


@pytest.mark.unit
def test_rate_limit_exhausted_reraises_rate_limit_error() -> None:
    sleep = _FakeSleep()
    policy = RetryPolicy(sleep=sleep, max_retries=3)
    fn = _flaky([RateLimitError("hit", status=429) for _ in range(3)])

    with pytest.raises(RateLimitError):
        policy.execute(fn)  # type: ignore[arg-type]
    # only 2 sleeps: no sleep after the final (3rd) failed attempt
    assert len(sleep.waits) == 2
    assert policy.last_attempts == 3


@pytest.mark.unit
def test_transient_backoff_is_exponential_capped() -> None:
    sleep = _FakeSleep()
    policy = RetryPolicy(sleep=sleep, transient_base_wait=2.0, transient_cap=10.0, max_retries=6)
    fn = _flaky([TransientError("boom", status=502) for _ in range(4)])

    assert policy.execute(fn) == "ok"  # type: ignore[arg-type]
    # 2, 4, 8, then capped at 10
    assert sleep.waits == [2.0, 4.0, 8.0, 10.0]


@pytest.mark.unit
def test_transient_exhausted_non_403_reraises_transient_error() -> None:
    sleep = _FakeSleep()
    policy = RetryPolicy(sleep=sleep, max_retries=2)
    fn = _flaky([TransientError("boom", status=503) for _ in range(2)])

    with pytest.raises(TransientError):
        policy.execute(fn)  # type: ignore[arg-type]


@pytest.mark.unit
def test_transient_403_exhausted_reraises_as_auth_error() -> None:
    """A 403-without-marker that never resolves after retries is treated as a bad token."""
    sleep = _FakeSleep()
    policy = RetryPolicy(sleep=sleep, max_retries=2)
    fn = _flaky([TransientError("blocked", status=403, body="abuse protection") for _ in range(2)])

    with pytest.raises(AuthError) as exc_info:
        policy.execute(fn)  # type: ignore[arg-type]
    assert exc_info.value.status == 403


@pytest.mark.unit
@pytest.mark.parametrize(
    "error", [AuthError("bad token", status=401), PermanentError("nope", status=404)]
)
def test_non_retryable_errors_propagate_immediately(error: Exception) -> None:
    sleep = _FakeSleep()
    policy = RetryPolicy(sleep=sleep, max_retries=5)
    fn = _flaky([error])

    with pytest.raises(type(error)):
        policy.execute(fn)  # type: ignore[arg-type]
    assert sleep.waits == []
    assert policy.last_attempts == 1


@pytest.mark.unit
def test_empty_body_error_propagates_immediately() -> None:
    sleep = _FakeSleep()
    policy = RetryPolicy(sleep=sleep, max_retries=5)
    fn = _flaky([EmptyBodyError("empty", status=200)])

    with pytest.raises(EmptyBodyError):
        policy.execute(fn)  # type: ignore[arg-type]
    assert sleep.waits == []


@pytest.mark.unit
def test_defer_rate_limits_raises_immediately_on_first_hit_no_sleep() -> None:
    """`defer_rate_limits=True` never retries a RateLimitError inline — it's
    the QuotaScheduler's job to re-queue it, not RetryPolicy's to sleep."""
    sleep = _FakeSleep()
    policy = RetryPolicy(sleep=sleep, max_retries=5, defer_rate_limits=True)
    fn = _flaky([RateLimitError("hit", status=403, body="Limit reached for x")])

    with pytest.raises(RateLimitError):
        policy.execute(fn)  # type: ignore[arg-type]
    assert sleep.waits == []
    assert policy.last_attempts == 1


@pytest.mark.unit
def test_defer_rate_limits_still_calls_on_rate_limit_once() -> None:
    sleep = _FakeSleep()
    seen: list[int] = []
    policy = RetryPolicy(
        sleep=sleep,
        defer_rate_limits=True,
        on_rate_limit=lambda exc, attempt: seen.append(attempt),
    )
    fn = _flaky([RateLimitError("hit", status=429)])

    with pytest.raises(RateLimitError):
        policy.execute(fn)  # type: ignore[arg-type]
    assert seen == [1]


@pytest.mark.unit
def test_defer_rate_limits_leaves_transient_backoff_inline() -> None:
    """Only RateLimitError is deferred — a 5xx still backs off inline,
    there's no per-key scheduling benefit to deferring a transient error."""
    sleep = _FakeSleep()
    policy = RetryPolicy(
        sleep=sleep,
        transient_base_wait=2.0,
        transient_cap=10.0,
        max_retries=6,
        defer_rate_limits=True,
    )
    fn = _flaky([TransientError("boom", status=502) for _ in range(4)])

    assert policy.execute(fn) == "ok"  # type: ignore[arg-type]
    assert sleep.waits == [2.0, 4.0, 8.0, 10.0]


@pytest.mark.unit
def test_max_retries_zero_still_attempts_once_then_raises() -> None:
    sleep = _FakeSleep()
    policy = RetryPolicy(sleep=sleep, max_retries=1)
    fn = _flaky([TransientError("boom", status=502)])

    with pytest.raises(TransientError):
        policy.execute(fn)  # type: ignore[arg-type]
    assert policy.last_attempts == 1
    assert sleep.waits == []
