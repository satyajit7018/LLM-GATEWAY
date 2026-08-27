"""Accounts, sessions, and bring-your-own provider keys.

Split out of main.py's original Phase 1 (auth) + Phase 2 (BYO keys) sections.
Keys live here rather than in gateway.py because they're account-scoped
settings (Depends(require_real_user), read/write this user's own data) —
generation itself only *reads* a key via state, it doesn't manage them.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import config, mailer, store
from ..llm import validate_key
from ..resilience import auth_bucket
from ..state import _COOKIE, _set_session_cookie, current_user, require_real_user

router = APIRouter()


def _guard_auth_rate(request: Request):
    """Security review finding: login/signup/forgot-password/resend had no
    throttling at all. Keyed by client IP (there's no user yet at login
    time) — a shared bucket across these four endpoints, so someone can't
    dodge the limit by bouncing between them."""
    ip = request.client.host if request.client else "unknown"
    if not auth_bucket.allow(f"auth:{ip}"):
        raise HTTPException(status_code=429, detail="too many attempts — wait a moment and try again")


class Credentials(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=1024)
    invite_code: str = Field(default="", max_length=64)   # only checked on signup


def _send_verification_email(user: dict):
    """Best-effort — signup must still succeed even if mail delivery is down
    or misconfigured; the user can always hit /auth/resend-verification."""
    token = store.create_verification_token(user["id"])
    link = f"{config.PUBLIC_BASE_URL}/?verify_token={token}"
    try:
        mailer.send_verification_email(user["email"], link)
    except Exception:
        pass


@router.post("/auth/signup")
def auth_signup(creds: Credentials, response: Response, request: Request):
    _guard_auth_rate(request)
    try:
        user = store.create_user(creds.email, creds.password)
    except store.UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if config.SIGNUP_REQUIRES_INVITE:
        if not store.redeem_invite_code(creds.invite_code, user["id"]):
            store._delete_user(user["id"])   # don't leave an unauthorized account behind
            raise HTTPException(status_code=400, detail="invalid or already-used invite code")
    _send_verification_email(user)
    _set_session_cookie(response, store.create_session(user["id"]))
    return {"user": user}


@router.post("/auth/login")
def auth_login(creds: Credentials, response: Response, request: Request):
    _guard_auth_rate(request)
    user = store.verify_user(creds.email, creds.password)
    if not user:
        raise HTTPException(status_code=401, detail="wrong email or password")
    _set_session_cookie(response, store.create_session(user["id"]))
    return {"user": user}


@router.post("/auth/logout")
def auth_logout(request: Request, response: Response):
    store.delete_session(request.cookies.get(_COOKIE))
    response.delete_cookie(_COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me")
def auth_me(request: Request):
    user = current_user(request)
    if user:
        user = {**user, "email_verified": store.is_email_verified(user["id"])}
    return {"user": user, "auth_required": config.AUTH_REQUIRED,
            "signup_requires_invite": config.SIGNUP_REQUIRES_INVITE}


class VerifyEmail(BaseModel):
    token: str = Field(max_length=256)


@router.post("/auth/verify-email")
def auth_verify_email(body: VerifyEmail):
    if store.verify_email_token(body.token) is None:
        raise HTTPException(status_code=400, detail="that verification link is invalid or has expired")
    return {"ok": True}


@router.post("/auth/resend-verification")
def auth_resend_verification(request: Request, user=Depends(require_real_user)):
    _guard_auth_rate(request)
    if store.is_email_verified(user["id"]):
        return {"ok": True, "already_verified": True}
    _send_verification_email(user)
    return {"ok": True, "already_verified": False}


class ChangePassword(BaseModel):
    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)


@router.post("/auth/change-password")
def auth_change_password(body: ChangePassword, request: Request, user=Depends(require_real_user)):
    if not store.verify_user_password(user["id"], body.current_password):
        raise HTTPException(status_code=400, detail="current password is incorrect")
    try:
        store.set_password(user["id"], body.new_password)
    except store.UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Keep this session alive (the user just proved who they are) but sign
    # every other device out — the same defensive move a reset does.
    store.delete_other_sessions(user["id"], request.cookies.get(_COOKIE))
    return {"ok": True}


@router.post("/auth/sign-out-everywhere")
def auth_sign_out_everywhere(response: Response, user=Depends(require_real_user)):
    store.delete_all_sessions(user["id"])
    response.delete_cookie(_COOKIE, path="/")   # this device too — "everywhere" means everywhere
    return {"ok": True}


class DeleteAccount(BaseModel):
    password: str = Field(max_length=1024)


@router.post("/auth/delete-account")
def auth_delete_account(body: DeleteAccount, response: Response, user=Depends(require_real_user)):
    if not store.verify_user_password(user["id"], body.password):
        raise HTTPException(status_code=400, detail="incorrect password")
    store.delete_account(user["id"])
    response.delete_cookie(_COOKIE, path="/")
    return {"ok": True}


class ForgotPassword(BaseModel):
    email: str = Field(max_length=254)


@router.post("/auth/forgot-password")
def auth_forgot_password(body: ForgotPassword, request: Request):
    """Always returns the same generic response whether or not the email is
    registered — otherwise this endpoint could be used to check who has an
    account (user enumeration)."""
    _guard_auth_rate(request)
    user = store.get_user_by_email(body.email)
    if user:
        token = store.create_reset_token(user["id"])
        link = f"{config.PUBLIC_BASE_URL}/?reset_token={token}"
        try:
            mailer.send_reset_email(user["email"], link)
        except Exception:
            pass   # never leak delivery failures to the caller — same generic response either way
    return {"ok": True, "message": "If that email has an account, a reset link has been sent."}


class ResetPassword(BaseModel):
    token: str = Field(max_length=256)
    new_password: str = Field(max_length=1024)


@router.post("/auth/reset-password")
def auth_reset_password(body: ResetPassword):
    user_id = store.user_id_for_reset_token(body.token)
    if user_id is None:
        raise HTTPException(status_code=400, detail="that reset link is invalid or has expired")
    try:
        store.set_password(user_id, body.new_password)
    except store.UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    store.consume_reset_token(body.token)   # single-use, and signs out any live sessions
    return {"ok": True}


# --- bring-your-own provider keys (hybrid build, Phase 2) -----------------
class KeyIn(BaseModel):
    provider: str = Field(max_length=32)
    api_key: str = Field(max_length=512)


@router.get("/keys")
def list_keys(user=Depends(require_real_user)):
    """This user's connected providers — never the actual key, just provider +
    last 4 characters, so the UI can show 'sk-…ab12' without re-exposing it."""
    if not store.keys_enabled():
        return {"enabled": False, "keys": []}
    return {"enabled": True, "keys": store.list_user_keys(user["id"])}


@router.post("/keys")
def add_key(body: KeyIn, user=Depends(require_real_user)):
    if body.provider not in config.PROVIDERS:
        raise HTTPException(status_code=400, detail=f"unknown provider '{body.provider}'")
    valid, err = validate_key(body.provider, body.api_key)
    if not valid:
        raise HTTPException(status_code=400, detail=err)
    try:
        store.set_user_key(user["id"], body.provider, body.api_key)
    except store.KeysDisabledError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except store.UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@router.delete("/keys/{provider}")
def remove_key(provider: str, user=Depends(require_real_user)):
    store.delete_user_key(user["id"], provider)
    return {"ok": True}
