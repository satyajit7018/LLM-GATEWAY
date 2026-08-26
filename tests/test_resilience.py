"""Rate limiter, retry/backoff, and circuit breaker tests."""
import time

import pytest

from app.llm import LLMError
from app.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    TokenBucket,
    retry_with_backoff,
)


def test_token_bucket_allows_burst_then_throttles():
    bucket = TokenBucket(rate_per_s=0, capacity=3)  # no refill
    allowed = sum(1 for _ in range(10) if bucket.allow())
    assert allowed == 3


def test_token_bucket_refills_over_time():
    bucket = TokenBucket(rate_per_s=1000, capacity=1)
    assert bucket.allow()
    assert not bucket.allow()          # bucket drained
    time.sleep(0.02)                   # ~20 tokens refill
    assert bucket.allow()


def test_retry_succeeds_after_transient_failures():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise LLMError("transient")
        return "ok"

    assert retry_with_backoff(flaky, max_retries=3, base_s=0.001) == "ok"
    assert calls["n"] == 3


def test_retry_reraises_after_exhausting():
    def always_fail():
        raise LLMError("nope")

    with pytest.raises(LLMError):
        retry_with_backoff(always_fail, max_retries=2, base_s=0.001)


def test_circuit_opens_then_recovers():
    cb = CircuitBreaker(fail_threshold=2, reset_s=0.2)
    for _ in range(2):
        cb.before_call()
        cb.record_failure()
    assert cb.state == "open"
    with pytest.raises(CircuitOpenError):
        cb.before_call()
    time.sleep(0.25)
    assert cb.state == "half_open"
    cb.before_call()
    cb.record_success()
    assert cb.state == "closed"
