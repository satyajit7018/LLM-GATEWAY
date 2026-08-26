"""Phase 4 tests: invite-gated signup, password reset, conversation sync."""
import uuid

from fastapi.testclient import TestClient

from app import config, mailer, store
from app.main import app


def _email():
    return f"p4{uuid.uuid4().hex[:12]}@example.com"


def _signed_in_client():
    c = TestClient(app)
    c.post("/auth/signup", json={"email": _email(), "password": "hunter2pass"})
    return c


# ---- invite-gated signup ----
def test_signup_open_by_default():
    assert config.SIGNUP_REQUIRES_INVITE is False
    c = TestClient(app)
    assert c.post("/auth/signup", json={"email": _email(), "password": "hunter2pass"}).status_code == 200


def test_signup_requires_valid_code_when_enabled(monkeypatch):
    monkeypatch.setattr(config, "SIGNUP_REQUIRES_INVITE", True)
    c = TestClient(app)
    email = _email()
    # no code / bad code -> rejected, and no account left behind
    r = c.post("/auth/signup", json={"email": email, "password": "hunter2pass"})
    assert r.status_code == 400
    assert store.get_user_by_email(email) is None

    code = store.create_invite_code()
    r2 = c.post("/auth/signup", json={"email": email, "password": "hunter2pass", "invite_code": code})
    assert r2.status_code == 200


def test_invite_code_is_single_use(monkeypatch):
    monkeypatch.setattr(config, "SIGNUP_REQUIRES_INVITE", True)
    code = store.create_invite_code()
    c1, c2 = TestClient(app), TestClient(app)
    e1, e2 = _email(), _email()
    assert c1.post("/auth/signup", json={"email": e1, "password": "hunter2pass", "invite_code": code}).status_code == 200
    r2 = c2.post("/auth/signup", json={"email": e2, "password": "hunter2pass", "invite_code": code})
    assert r2.status_code == 400
    assert store.get_user_by_email(e2) is None   # rolled back, not left dangling


def test_me_reports_invite_requirement(monkeypatch):
    monkeypatch.setattr(config, "SIGNUP_REQUIRES_INVITE", True)
    d = TestClient(app).get("/auth/me").json()
    assert d["signup_requires_invite"] is True


# ---- password reset ----
def test_forgot_password_same_response_regardless_of_existence(monkeypatch):
    sent = {}
    monkeypatch.setattr(mailer, "send_reset_email", lambda to, link: sent.update(to=to, link=link))
    c = TestClient(app)
    email = _email()
    c.post("/auth/signup", json={"email": email, "password": "hunter2pass"})

    r1 = c.post("/auth/forgot-password", json={"email": email})
    r2 = c.post("/auth/forgot-password", json={"email": "no-such-user@example.com"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["message"] == r2.json()["message"]   # no user-enumeration signal
    assert sent["to"] == email   # only the real account actually got a link


def test_reset_password_flow_and_token_is_single_use(monkeypatch):
    monkeypatch.setattr(mailer, "send_reset_email", lambda to, link: None)
    c = TestClient(app)
    email = _email()
    c.post("/auth/signup", json={"email": email, "password": "hunter2pass"})
    user = store.get_user_by_email(email)
    token = store.create_reset_token(user["id"])

    r = c.post("/auth/reset-password", json={"token": token, "new_password": "brandnewpass1"})
    assert r.status_code == 200

    # old password no longer works, new one does
    fresh = TestClient(app)
    assert fresh.post("/auth/login", json={"email": email, "password": "hunter2pass"}).status_code == 401
    assert fresh.post("/auth/login", json={"email": email, "password": "brandnewpass1"}).status_code == 200

    # token can't be reused
    r2 = c.post("/auth/reset-password", json={"token": token, "new_password": "anotherpass2"})
    assert r2.status_code == 400


def test_reset_password_invalidates_existing_sessions():
    c = TestClient(app)
    email = _email()
    c.post("/auth/signup", json={"email": email, "password": "hunter2pass"})
    assert c.get("/auth/me").json()["user"] is not None   # session live

    user = store.get_user_by_email(email)
    token = store.create_reset_token(user["id"])
    c.post("/auth/reset-password", json={"token": token, "new_password": "newpassword1"})

    assert c.get("/auth/me").json()["user"] is None   # old session was signed out


def test_reset_password_bad_token_rejected():
    c = TestClient(app)
    r = c.post("/auth/reset-password", json={"token": "not-a-real-token", "new_password": "whatever1"})
    assert r.status_code == 400


# ---- conversation sync ----
def test_sync_requires_real_session():
    anon = TestClient(app)
    assert anon.get("/sync/store").status_code == 401
    assert anon.put("/sync/store", json={"data": "{}"}).status_code == 401


def test_sync_pull_empty_before_any_push():
    c = _signed_in_client()
    assert c.get("/sync/store").json() == {"data": None}


def test_sync_push_then_pull_roundtrip():
    c = _signed_in_client()
    payload = '{"conversations": [{"id": "abc", "messages": []}]}'
    assert c.put("/sync/store", json={"data": payload}).status_code == 200
    assert c.get("/sync/store").json() == {"data": payload}


def test_sync_is_isolated_per_user():
    a, b = _signed_in_client(), _signed_in_client()
    a.put("/sync/store", json={"data": '{"mine": "a"}'})
    b.put("/sync/store", json={"data": '{"mine": "b"}'})
    assert a.get("/sync/store").json()["data"] == '{"mine": "a"}'
    assert b.get("/sync/store").json()["data"] == '{"mine": "b"}'
