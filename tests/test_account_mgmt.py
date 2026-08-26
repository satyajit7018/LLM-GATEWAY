"""Account-management nice-to-haves: change password, sign out everywhere,
account deletion, email verification. None of these were part of the
original phased plan — added after a self-audit of what a logged-in user
should be able to do to their own account."""
import uuid

from fastapi.testclient import TestClient

from app import store
from app.main import app


def _email():
    return f"a{uuid.uuid4().hex[:12]}@example.com"


def _signed_up():
    c = TestClient(app)
    email = _email()
    r = c.post("/auth/signup", json={"email": email, "password": "hunter2pass"})
    return c, email, r.json()["user"]["id"]


# ---- change password ----
def test_change_password_wrong_current_rejected():
    c, email, uid = _signed_up()
    r = c.post("/auth/change-password", json={"current_password": "nope", "new_password": "newpass123"})
    assert r.status_code == 400


def test_change_password_updates_and_signs_out_other_sessions():
    c, email, uid = _signed_up()
    other = TestClient(app)
    other.post("/auth/login", json={"email": email, "password": "hunter2pass"})
    assert other.get("/auth/me").json()["user"] is not None

    r = c.post("/auth/change-password", json={"current_password": "hunter2pass", "new_password": "newpass123"})
    assert r.status_code == 200
    # the session that made the change stays logged in...
    assert c.get("/auth/me").json()["user"] is not None
    # ...but the other device's session was killed
    assert other.get("/auth/me").json()["user"] is None
    # old password no longer works, new one does
    assert TestClient(app).post("/auth/login", json={"email": email, "password": "hunter2pass"}).status_code == 401
    assert TestClient(app).post("/auth/login", json={"email": email, "password": "newpass123"}).status_code == 200


def test_change_password_requires_real_session():
    anon = TestClient(app)
    assert anon.post("/auth/change-password", json={"current_password": "x", "new_password": "newpass123"}).status_code == 401


# ---- sign out everywhere ----
def test_sign_out_everywhere_kills_all_sessions_including_current():
    c, email, uid = _signed_up()
    other = TestClient(app)
    other.post("/auth/login", json={"email": email, "password": "hunter2pass"})

    r = c.post("/auth/sign-out-everywhere")
    assert r.status_code == 200
    assert c.get("/auth/me").json()["user"] is None
    assert other.get("/auth/me").json()["user"] is None


# ---- account deletion ----
def test_delete_account_requires_correct_password():
    c, email, uid = _signed_up()
    assert c.post("/auth/delete-account", json={"password": "wrong"}).status_code == 400
    assert store.get_user_by_email(email) is not None   # untouched


def test_delete_account_removes_user_and_signs_out():
    c, email, uid = _signed_up()
    r = c.post("/auth/delete-account", json={"password": "hunter2pass"})
    assert r.status_code == 200
    assert store.get_user_by_email(email) is None
    assert c.get("/auth/me").json()["user"] is None
    assert TestClient(app).post("/auth/login", json={"email": email, "password": "hunter2pass"}).status_code == 401


# ---- email verification ----
def test_signup_issues_unverified_account():
    c, email, uid = _signed_up()
    assert c.get("/auth/me").json()["user"]["email_verified"] is False


def test_verify_email_token_marks_account_verified():
    c, email, uid = _signed_up()
    token = store.create_verification_token(uid)   # signup already sent one; issue a fresh one to control it
    r = c.post("/auth/verify-email", json={"token": token})
    assert r.status_code == 200
    assert c.get("/auth/me").json()["user"]["email_verified"] is True


def test_verify_email_bad_token_rejected():
    c, email, uid = _signed_up()
    r = c.post("/auth/verify-email", json={"token": "not-a-real-token"})
    assert r.status_code == 400


def test_verify_email_token_is_single_use():
    c, email, uid = _signed_up()
    token = store.create_verification_token(uid)
    assert c.post("/auth/verify-email", json={"token": token}).status_code == 200
    assert c.post("/auth/verify-email", json={"token": token}).status_code == 400


def test_resend_verification_noop_once_verified():
    c, email, uid = _signed_up()
    store.verify_email_token(store.create_verification_token(uid))
    r = c.post("/auth/resend-verification")
    assert r.status_code == 200 and r.json()["already_verified"] is True


def test_resend_verification_requires_real_session():
    anon = TestClient(app)
    assert anon.post("/auth/resend-verification").status_code == 401
