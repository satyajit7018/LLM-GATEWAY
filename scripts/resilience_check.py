"""Deterministic check of the Step 5 resilience primitives (no server needed).

Verifies:
  1. token-bucket rate limiter allows a burst then throttles
  2. retry_with_backoff retries transient failures then succeeds
  3. circuit breaker opens after N failures and half-opens after cooldown

Run:  python -m scripts.resilience_check
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.llm import LLMError  # noqa: E402
from app.resilience import (  # noqa: E402
    CircuitBreaker,
    CircuitOpenError,
    TokenBucket,
    retry_with_backoff,
)


def check_rate_limiter():
    bucket = TokenBucket(rate_per_s=10, capacity=5)
    allowed = sum(1 for _ in range(20) if bucket.allow())
    assert allowed == 5, f"expected 5 through the burst, got {allowed}"
    print(f"1. rate limiter: 5/20 burst requests allowed, 15 throttled  OK")


def check_retry():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise LLMError("transient")
        return "ok"

    result = retry_with_backoff(flaky, max_retries=3, base_s=0.001)
    assert result == "ok" and calls["n"] == 3
    print(f"2. retry/backoff: failed twice, succeeded on attempt 3  OK")


def check_circuit():
    cb = CircuitBreaker(fail_threshold=3, reset_s=0.3)
    for _ in range(3):
        cb.before_call()
        cb.record_failure()
    assert cb.state == "open"
    try:
        cb.before_call()
        raise AssertionError("expected CircuitOpenError")
    except CircuitOpenError:
        pass
    time.sleep(0.35)
    assert cb.state == "half_open"
    cb.before_call()
    cb.record_success()
    assert cb.state == "closed"
    print(f"3. circuit breaker: opened after 3 fails, half-opened, recovered  OK")


if __name__ == "__main__":
    check_rate_limiter()
    check_retry()
    check_circuit()
    print("\nAll Step 5 resilience checks passed.")
