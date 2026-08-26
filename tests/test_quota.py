"""Free-tier per-user daily quota tests (hybrid build, Phase 3)."""
import uuid

from fastapi.testclient import TestClient

from app import config, store
from app.main import app


def _email():
    return f"q{uuid.uuid4().hex[:12]}@example.com"


def _signed_in_client():
    c = TestClient(app)
    c.post("/auth/signup", json={"email": _email(), "password": "hunter2pass"})
    return c


def _fresh_prompt():
    """A prompt no other test could have cached — the exact/semantic caches
    are global singletons keyed on prompt+model, not per-user, so a shared
    prompt like "hi" can hit another test's cached entry and skip the quota
    check entirely (that's correct behavior — cache hits are free — but it
    would make these tests flaky based on run order)."""
    return f"quota test prompt {uuid.uuid4().hex}"


# ---- store.py: usage bookkeeping ----
def test_usage_starts_at_zero():
    assert store.usage_today(888001) == {"tokens": 0, "requests": 0}


def test_record_usage_accumulates():
    store.record_usage(888002, 100)
    store.record_usage(888002, 50)
    assert store.usage_today(888002) == {"tokens": 150, "requests": 2}


def test_usage_is_per_user():
    store.record_usage(888003, 100)
    store.record_usage(888004, 999)
    assert store.usage_today(888003)["tokens"] == 100
    assert store.usage_today(888004)["tokens"] == 999


# ---- GET /usage/free ----
def test_usage_free_requires_real_session():
    anon = TestClient(app)
    assert anon.get("/usage/free").status_code == 401


def test_usage_free_reports_limit_and_used(monkeypatch):
    monkeypatch.setattr(config, "FREE_DAILY_TOKEN_LIMIT", 1000)
    c = _signed_in_client()
    d = c.get("/usage/free").json()
    assert d == {"unlimited": False, "limit": 1000, "used": 0, "remaining": 1000}


def test_usage_free_unlimited_when_limit_zero(monkeypatch):
    monkeypatch.setattr(config, "FREE_DAILY_TOKEN_LIMIT", 0)
    c = _signed_in_client()
    d = c.get("/usage/free").json()
    assert d["unlimited"] is True and d["remaining"] is None


# ---- enforcement: /generate rejects once the daily cap is used up ----
def test_generate_blocked_once_quota_exhausted(monkeypatch):
    monkeypatch.setattr(config, "AUTH_REQUIRED", True)
    monkeypatch.setattr(config, "FREE_DAILY_TOKEN_LIMIT", 10)
    c = _signed_in_client()

    # Mock backend responses are small (well under 10 tokens is unlikely, so
    # force the accounting directly to make this deterministic and fast).
    me = c.get("/auth/me").json()["user"]
    store.record_usage(me["id"], 10)   # already at the cap

    r = c.post("/generate", json={"prompt": _fresh_prompt()})
    assert r.status_code == 429
    assert "Free daily limit" in r.json()["detail"]


def test_generate_succeeds_under_quota(monkeypatch):
    monkeypatch.setattr(config, "AUTH_REQUIRED", True)
    monkeypatch.setattr(config, "FREE_DAILY_TOKEN_LIMIT", 1_000_000)
    c = _signed_in_client()
    r = c.post("/generate", json={"prompt": _fresh_prompt()})
    assert r.status_code == 200
    me = c.get("/auth/me").json()["user"]
    assert store.usage_today(me["id"])["tokens"] > 0   # the mock call's tokens were charged


def test_byo_key_calls_do_not_count_against_quota(monkeypatch):
    """A user with their own key for a model's provider should never be quota-
    limited on that model, even at $0 remaining free allowance."""
    monkeypatch.setattr(config, "APP_ENCRYPTION_KEY", "0" * 43 + "=")
    store._fernet_cache = None
    monkeypatch.setattr(config, "AUTH_REQUIRED", True)
    monkeypatch.setattr(config, "FREE_DAILY_TOKEN_LIMIT", 1)  # essentially zero allowance
    c = _signed_in_client()
    me = c.get("/auth/me").json()["user"]
    store.record_usage(me["id"], 999999)  # far over the cap already

    # Mock backend ignores the model/provider distinction, but the BYO-key
    # branch is what matters here: it must bypass the quota check entirely.
    c.post("/keys", json={"provider": "groq", "api_key": "sk-my-own-groq-key"})
    r = c.post("/generate", json={"prompt": _fresh_prompt(), "model": "groq/gpt-oss-20b"})
    assert r.status_code == 200
    store._fernet_cache = None
