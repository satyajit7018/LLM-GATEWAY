"""Bring-your-own provider key tests (hybrid build, Phase 2): storage,
encryption, the /keys API, /models unlocking, and key resolution in llm.py.
"""
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, llm, store
from app.main import app


class _FakeResp:
    def __init__(self, status_code):
        self.status_code = status_code


def _email():
    return f"k{uuid.uuid4().hex[:12]}@example.com"


def _signed_in_client():
    c = TestClient(app)
    c.post("/auth/signup", json={"email": _email(), "password": "hunter2pass"})
    return c


@pytest.fixture(autouse=True)
def _fernet_key(monkeypatch):
    """A real (test) encryption key so BYO-key storage is exercised, not just
    its disabled path. A fixed value keeps this deterministic."""
    monkeypatch.setattr(config, "APP_ENCRYPTION_KEY", "0" * 43 + "=")
    store._fernet_cache = None   # the module caches the Fernet instance; reset it
    yield
    store._fernet_cache = None


# ---- store.py: storage + encryption ----
def test_set_get_roundtrip_and_masking():
    store.set_user_key(999001, "openai", "sk-supersecretvalue")
    assert store.get_user_key(999001, "openai") == "sk-supersecretvalue"
    keys = store.list_user_keys(999001)
    assert keys == [{"provider": "openai", "last4": "alue"}]


def test_overwrite_same_provider_updates_key():
    store.set_user_key(999002, "openai", "sk-first")
    store.set_user_key(999002, "openai", "sk-second")
    assert store.get_user_key(999002, "openai") == "sk-second"
    assert len(store.list_user_keys(999002)) == 1


def test_delete_key():
    store.set_user_key(999003, "mistral", "abcd1234")
    store.delete_user_key(999003, "mistral")
    assert store.get_user_key(999003, "mistral") is None
    assert store.list_user_keys(999003) == []


def test_empty_key_rejected():
    with pytest.raises(store.UserError):
        store.set_user_key(999004, "openai", "   ")


def test_keys_disabled_without_encryption_key(monkeypatch):
    monkeypatch.setattr(config, "APP_ENCRYPTION_KEY", "")
    store._fernet_cache = None
    assert store.keys_enabled() is False
    with pytest.raises(store.KeysDisabledError):
        store.set_user_key(999005, "openai", "sk-x")


# ---- /keys HTTP API ----
def test_keys_requires_auth():
    anon = TestClient(app)
    assert anon.get("/keys").status_code == 401
    assert anon.post("/keys", json={"provider": "openai", "api_key": "sk-x"}).status_code == 401


def test_add_list_delete_via_api():
    c = _signed_in_client()
    assert c.get("/keys").json() == {"enabled": True, "keys": []}

    r = c.post("/keys", json={"provider": "openai", "api_key": "sk-abcd1234"})
    assert r.status_code == 200 and r.json()["ok"] is True

    listed = c.get("/keys").json()
    assert listed["keys"] == [{"provider": "openai", "last4": "1234"}]

    r = c.delete("/keys/openai")
    assert r.status_code == 200
    assert c.get("/keys").json()["keys"] == []


def test_unknown_provider_rejected():
    c = _signed_in_client()
    r = c.post("/keys", json={"provider": "not-a-real-provider", "api_key": "sk-x"})
    assert r.status_code == 400


# ---- /models reflects per-user unlocking ----
def test_models_show_user_source_after_adding_key(monkeypatch):
    # Pick a provider the app itself has no key for, so it starts locked.
    monkeypatch.setattr(config, "LLM_BACKEND", "groq")  # avoid the mock-backend "always available" path
    monkeypatch.setattr(config, "PROVIDERS", {**config.PROVIDERS,
                        "mistral": {**config.PROVIDERS["mistral"], "key_env": "NOPE_UNSET_ENV"}})
    monkeypatch.setattr("app.main.validate_key", lambda p, k: (True, None))  # unrelated to this test
    c = _signed_in_client()
    before = next(m for m in c.get("/models").json()["models"] if m["provider"] == "mistral")
    assert before["available"] is False and before["source"] is None

    c.post("/keys", json={"provider": "mistral", "api_key": "sk-mistral-key"})
    after = next(m for m in c.get("/models").json()["models"] if m["provider"] == "mistral")
    assert after["available"] is True and after["source"] == "user"


# ---- llm.py: key validation (nice-to-have #1 — catch a bad BYO key at save
# time instead of letting it fail silently on first real use) ----
def test_validate_key_skips_call_in_mock_backend():
    # config.LLM_BACKEND defaults to "mock" for the whole test suite — nothing
    # to call, so a key is accepted without hitting the network.
    valid, err = llm.validate_key("groq", "anything")
    assert valid is True and err is None


def test_validate_key_rejects_401(monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "groq")
    monkeypatch.setattr(llm._client, "post", lambda *a, **k: _FakeResp(401))
    valid, err = llm.validate_key("groq", "sk-bad")
    assert valid is False
    assert "rejected" in err.lower()


def test_validate_key_accepts_200(monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "groq")
    monkeypatch.setattr(llm._client, "post", lambda *a, **k: _FakeResp(200))
    valid, err = llm.validate_key("groq", "sk-good")
    assert valid is True and err is None


def test_validate_key_does_not_block_on_network_error(monkeypatch):
    # A timeout or connection failure isn't proof the key is bad — don't
    # punish the user for a transient provider/network hiccup.
    monkeypatch.setattr(config, "LLM_BACKEND", "groq")
    def _raise(*a, **k): raise httpx.ConnectError("no route")
    monkeypatch.setattr(llm._client, "post", _raise)
    valid, err = llm.validate_key("groq", "sk-x")
    assert valid is True


def test_validate_key_does_not_block_on_rate_limit(monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "groq")
    monkeypatch.setattr(llm._client, "post", lambda *a, **k: _FakeResp(429))
    valid, err = llm.validate_key("groq", "sk-x")
    assert valid is True


def test_add_key_endpoint_rejects_invalid_key(monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "groq")
    monkeypatch.setattr("app.main.validate_key", lambda p, k: (False, "That key was rejected."))
    c = _signed_in_client()
    r = c.post("/keys", json={"provider": "groq", "api_key": "sk-bad"})
    assert r.status_code == 400
    assert c.get("/keys").json()["keys"] == []   # never stored


# ---- llm.py: key resolution ----
def test_resolve_prefers_byo_key_over_app_key():
    r = llm._resolve("groq/gpt-oss-20b", has_images=False, key_override="sk-user-owns-this")
    assert r["key"] == "sk-user-owns-this"
    assert r["byo"] is True


def test_resolve_falls_back_to_app_key_without_override(monkeypatch):
    monkeypatch.setattr(config, "provider_key", lambda p: "sk-app-shared-key")
    r = llm._resolve("groq/gpt-oss-20b", has_images=False)
    assert r["key"] == "sk-app-shared-key"
    assert r["byo"] is False
