"""Regression tests for the pre-launch security review: three real findings,
each fixed in place rather than just reported.

1. /admin/reset was public and unauthenticated (anyone could wipe the shared
   cache/counters) — now loopback-only.
2. /auth/login, /auth/signup, /auth/forgot-password, /auth/resend-verification
   had no throttling at all (unlimited password guessing; inbox-spam via
   forgot/resend) — now share a tight, IP-keyed rate limit.
3. /p/{slug} served arbitrary user-authored HTML/JS at the app's own origin
   with no isolation — a malicious published page could otherwise ride an
   already-logged-in visitor's session cookie to call the app's own API as
   them. Now carries a CSP that blocks exactly that (connect-src/form-action
   'none') while leaving the page's own inline JS free to run.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.resilience import auth_bucket

client = TestClient(app)


def _email():
    return f"sec{uuid.uuid4().hex[:12]}@example.com"


# ---- finding 1: /admin/reset ----
def test_admin_reset_blocked_for_non_loopback_client():
    # TestClient presents a non-loopback host ("testclient"), same signal
    # the /run guard's own tests rely on to prove that block works.
    r = client.post("/admin/reset")
    assert r.status_code == 403


# ---- finding 2: auth rate limiting ----
@pytest.fixture(autouse=True)
def _fresh_auth_bucket():
    # Buckets persist across tests otherwise, so an earlier test's attempts
    # would make a later, unrelated test flaky by starting already-throttled.
    auth_bucket._buckets.clear()
    yield
    auth_bucket._buckets.clear()


def test_login_is_rate_limited_after_a_burst(monkeypatch):
    # auth_bucket is a module-level singleton built from config at import
    # time, so patching config here wouldn't reach it — patch the bucket's
    # own rate/capacity instead (no refill mid-test, so this is deterministic).
    monkeypatch.setattr(auth_bucket, "_rate", 0.0)
    monkeypatch.setattr(auth_bucket, "_capacity", 3)
    email, password = _email(), "hunter2pass"
    client.post("/auth/signup", json={"email": email, "password": password})
    auth_bucket._buckets.clear()   # signup itself spent from the same bucket — start the login count fresh
    for _ in range(3):
        r = client.post("/auth/login", json={"email": email, "password": "wrong"})
        assert r.status_code == 401   # burst still allowed through (as real failed attempts)
    limited = client.post("/auth/login", json={"email": email, "password": "wrong"})
    assert limited.status_code == 429


def test_forgot_password_is_rate_limited_too(monkeypatch):
    # Same shared bucket as login — otherwise someone could dodge the limit
    # by bouncing between endpoints instead of hammering just one.
    monkeypatch.setattr(auth_bucket, "_rate", 0.0)
    monkeypatch.setattr(auth_bucket, "_capacity", 2)
    for _ in range(2):
        assert client.post("/auth/forgot-password", json={"email": "nobody@example.com"}).status_code == 200
    assert client.post("/auth/forgot-password", json={"email": "nobody@example.com"}).status_code == 429


# ---- finding 3: published-page CSP ----
def test_published_page_carries_a_locked_down_csp():
    r = client.post("/publish", json={"html": "<h1>hi</h1>", "conv_id": None})
    assert r.status_code == 200
    slug = r.json()["slug"]
    page = client.get(f"/p/{slug}")
    assert page.status_code == 200
    csp = page.headers.get("content-security-policy", "")
    assert "connect-src 'none'" in csp
    assert "form-action 'none'" in csp
