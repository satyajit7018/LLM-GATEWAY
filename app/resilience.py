"""Retry/backoff + circuit breaker + token-bucket rate limiter (Step 5).

Kept dependency-free (no slowapi) so the app installs and runs anywhere. The
circuit breaker stops hammering a down backend; retries with exponential backoff
absorb transient blips; the token bucket caps sustained throughput and bursts.
"""
import threading
import time
from collections import OrderedDict

from . import config
from .llm import LLMError


class CircuitOpenError(RuntimeError):
    pass


class CircuitBreaker:
    """CLOSED -> (failures) -> OPEN -> (cooldown) -> HALF_OPEN -> CLOSED."""

    def __init__(self, fail_threshold: int, reset_s: float):
        self._fail_threshold = fail_threshold
        self._reset_s = reset_s
        self._failures = 0
        self._opened_at = 0.0
        self._state = "closed"
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _maybe_half_open(self):
        if self._state == "open" and time.time() - self._opened_at >= self._reset_s:
            self._state = "half_open"

    def before_call(self):
        with self._lock:
            self._maybe_half_open()
            if self._state == "open":
                raise CircuitOpenError(
                    "circuit open: backend recently failed, not calling it"
                )

    def record_success(self):
        with self._lock:
            self._failures = 0
            self._state = "closed"

    def record_failure(self):
        with self._lock:
            self._failures += 1
            if self._failures >= self._fail_threshold:
                self._state = "open"
                self._opened_at = time.time()


def retry_with_backoff(fn, *, max_retries: int, base_s: float):
    """Call fn(), retrying on LLMError with exponential backoff. Re-raises last."""
    attempt = 0
    while True:
        try:
            return fn()
        except LLMError:
            if attempt >= max_retries:
                raise
            time.sleep(base_s * (2 ** attempt))
            attempt += 1


class TokenBucket:
    def __init__(self, rate_per_s: float, capacity: float):
        self._rate = rate_per_s
        self._capacity = capacity
        self._tokens = capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def allow(self, cost: float = 1.0) -> bool:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(
                self._capacity, self._tokens + (now - self._last) * self._rate
            )
            self._last = now
            if self._tokens >= cost:
                self._tokens -= cost
                return True
            return False


class KeyedTokenBuckets:
    """One TokenBucket per key (e.g. per logged-in user, or per client IP when
    nobody's logged in) — hybrid multi-user build. Without this, a single
    global bucket lets one busy account exhaust the whole app's rate-limit
    budget and 429 everyone else. Bounded to `max_keys` with FIFO eviction
    (mirrors the pattern in cache.py's in-memory backend) so long uptime with
    many distinct users doesn't grow this dict forever.
    """

    def __init__(self, rate_per_s: float, capacity: float, max_keys: int = 5000):
        self._rate = rate_per_s
        self._capacity = capacity
        self._max_keys = max_keys
        self._buckets: "OrderedDict[str, TokenBucket]" = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key: str, cost: float = 1.0) -> bool:
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = TokenBucket(self._rate, self._capacity)
                self._buckets[key] = b
                while len(self._buckets) > self._max_keys:
                    self._buckets.popitem(last=False)  # evict the oldest key
            else:
                self._buckets.move_to_end(key)
        return b.allow(cost)   # TokenBucket has its own lock; no need to hold ours here


# Module-level singletons wired from config.
breaker = CircuitBreaker(config.CIRCUIT_FAIL_THRESHOLD, config.CIRCUIT_RESET_S)
bucket = TokenBucket(config.RATE_LIMIT_RPS, config.RATE_LIMIT_BURST)
per_user_bucket = KeyedTokenBuckets(config.PER_USER_RATE_LIMIT_RPS, config.PER_USER_RATE_LIMIT_BURST)
# Security review finding — see config.py's AUTH_RATE_LIMIT_* comment.
auth_bucket = KeyedTokenBuckets(config.AUTH_RATE_LIMIT_RPS, config.AUTH_RATE_LIMIT_BURST)


def resilient_generate(generate_fn, prompt: str) -> dict:
    """Run generate_fn(prompt) behind the circuit breaker + retry/backoff."""
    breaker.before_call()  # raises CircuitOpenError if open
    try:
        result = retry_with_backoff(
            lambda: generate_fn(prompt),
            max_retries=config.LLM_MAX_RETRIES,
            base_s=config.LLM_BACKOFF_BASE_S,
        )
    except LLMError:
        breaker.record_failure()
        raise
    breaker.record_success()
    return result
