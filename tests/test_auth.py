"""Auth foundation tests (Phase 1): signup, login, sessions, gating."""
import uuid

from fastapi.testclient import TestClient

from app import config
from app.main import app


def _email():
    return f"u{uuid.uuid4().hex[:12]}@example.com"


def test_signup_sets_session_and_me_returns_user():
    c = TestClient(app)
    email = _email()
    r = c.post("/auth/signup", json={"email": email, "password": "hunter2pass"})
    assert r.status_code == 200
    assert r.json()["user"]["email"] == email
    assert "session" in r.cookies
    me = c.get("/auth/me").json()
    assert me["user"]["email"] == email


def test_duplicate_email_rejected():
    c = TestClient(app)
    email = _email()
    assert c.post("/auth/signup", json={"email": email, "password": "hunter2pass"}).status_code == 200
    r = c.post("/auth/signup", json={"email": email, "password": "hunter2pass"})
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"]


def test_short_password_rejected():
    c = TestClient(app)
    r = c.post("/auth/signup", json={"email": _email(), "password": "short"})
    assert r.status_code == 400


def test_login_wrong_password_401_then_correct_ok():
    c = TestClient(app)
    email = _email()
    c.post("/auth/signup", json={"email": email, "password": "hunter2pass"})
    c.post("/auth/logout")
    assert c.post("/auth/login", json={"email": email, "password": "nope"}).status_code == 401
    r = c.post("/auth/login", json={"email": email, "password": "hunter2pass"})
    assert r.status_code == 200 and r.json()["user"]["email"] == email


def test_logout_clears_session():
    c = TestClient(app)
    c.post("/auth/signup", json={"email": _email(), "password": "hunter2pass"})
    assert c.get("/auth/me").json()["user"] is not None
    c.post("/auth/logout")
    assert c.get("/auth/me").json()["user"] is None


def test_generate_gated_when_auth_required(monkeypatch):
    monkeypatch.setattr(config, "AUTH_REQUIRED", True)
    anon = TestClient(app)
    assert anon.post("/generate", json={"prompt": "hi"}).status_code == 401
    authed = TestClient(app)
    authed.post("/auth/signup", json={"email": _email(), "password": "hunter2pass"})
    assert authed.post("/generate", json={"prompt": "hi"}).status_code == 200
