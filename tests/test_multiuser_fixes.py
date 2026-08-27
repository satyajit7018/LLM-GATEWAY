"""Tests for two correctness gaps the multi-user (hybrid) build introduced:
the shared rate limiter and the un-scoped request log — fixed after the fact,
not part of the original phased plan.
"""
import uuid

from fastapi.testclient import TestClient

from app import resilience
from app.main import app


def _email():
    return f"m{uuid.uuid4().hex[:12]}@example.com"


def _signed_in_client():
    c = TestClient(app)
    c.post("/auth/signup", json={"email": _email(), "password": "hunter2pass"})
    return c


def _fresh_prompt():
    """Unique per call, with NO shared natural-language words across calls.
    The exact cache matches identical strings, and the semantic cache matches
    near-duplicate *wording* — so even "unique" prompts that share a common
    phrase (e.g. "test message <uuid>") can register as near-duplicates of
    each other and hit the semantic cache instead of the fresh-call path a
    test means to exercise. A bare hex string shares no tokens with anything."""
    return uuid.uuid4().hex


# ---- per-account rate limiting ----
def test_per_user_bucket_isolates_accounts(monkeypatch):
    """Each account gets its OWN budget — one user hitting their cap must not
    affect a different user's ability to send requests."""
    tiny = resilience.KeyedTokenBuckets(rate_per_s=0.0001, capacity=1)  # ~1 request, no meaningful refill
    monkeypatch.setattr("app.routers.gateway.per_user_bucket", tiny)

    a, b, c = _signed_in_client(), _signed_in_client(), _signed_in_client()
    # Each account's FIRST call succeeds — if they shared one bucket instead of
    # one each, b's or c's first call would already be starved by a's.
    assert a.post("/generate", json={"prompt": _fresh_prompt()}).status_code == 200
    assert b.post("/generate", json={"prompt": _fresh_prompt()}).status_code == 200
    assert c.post("/generate", json={"prompt": _fresh_prompt()}).status_code == 200

    # a's SECOND call exceeds their own tiny per-account cap.
    r = a.post("/generate", json={"prompt": _fresh_prompt()})
    assert r.status_code == 429
    assert "too fast" in r.json()["detail"]

    # b and c are completely unaffected by a being rate-limited — a fresh
    # account (d) can still make its first call too.
    d = _signed_in_client()
    assert d.post("/generate", json={"prompt": _fresh_prompt()}).status_code == 200


def test_global_bucket_still_applies_on_top(monkeypatch):
    """The per-user fix is additive — the original global cap (protecting the
    upstream provider in aggregate) must still be enforced."""
    monkeypatch.setattr("app.routers.gateway.bucket", resilience.TokenBucket(rate_per_s=0.0001, capacity=0))
    c = _signed_in_client()
    r = c.post("/generate", json={"prompt": _fresh_prompt()})
    assert r.status_code == 429
    assert r.json()["detail"] == "rate limit exceeded"   # the global message, not the per-user one


# ---- per-user request log scoping ----
def test_log_usage_requires_real_session():
    anon = TestClient(app)
    assert anon.get("/log/usage").status_code == 401


def test_log_usage_scoped_per_user_not_global():
    a, b = _signed_in_client(), _signed_in_client()
    a.post("/generate", json={"prompt": _fresh_prompt(), "model": "groq/gpt-oss-20b"})
    b.post("/generate", json={"prompt": _fresh_prompt(), "model": "groq/gpt-oss-20b"})
    b.post("/generate", json={"prompt": _fresh_prompt(), "model": "groq/gpt-oss-20b"})

    usage_a = a.get("/log/usage").json()["by_model"].get("groq/gpt-oss-20b", {"requests": 0})
    usage_b = b.get("/log/usage").json()["by_model"].get("groq/gpt-oss-20b", {"requests": 0})
    assert usage_a["requests"] == 1     # a's own single call — not b's two
    assert usage_b["requests"] == 2     # b's own two calls — not mixed with a's
