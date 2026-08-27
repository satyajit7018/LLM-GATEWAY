"""User accounts + sessions + bring-your-own provider keys (hybrid build,
Phases 1-2).

SQLite-backed, dependency-free for accounts. Passwords are hashed with
PBKDF2-HMAC-SHA256 (stdlib) using a per-user salt. Session tokens are random
and opaque; only their hash is stored, so a DB leak doesn't hand out live
sessions. Users' own provider API keys are encrypted at rest with Fernet
(symmetric AES) — see `_fernet()`.
"""
from __future__ import annotations   # PEP 604 `X | None` hints on Python 3.9

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time

from . import config

_lock = threading.Lock()
_conn = None

_PBKDF2_ITERS = 240_000
_SESSION_TTL_S = 60 * 60 * 24 * 30  # 30 days


def _connect():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.AUTH_DB_PATH, check_same_thread=False)
        # WAL lets reads (login, session checks) proceed without blocking
        # behind a write — real benefit now that multiple devices sync
        # against this same account concurrently. No-ops on ":memory:"
        # (used in tests).
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at REAL NOT NULL,
            email_verified INTEGER NOT NULL DEFAULT 0)""")
        # migration: older DBs were created before email verification existed
        user_cols = [r[1] for r in _conn.execute("PRAGMA table_info(users)").fetchall()]
        if "email_verified" not in user_cols:
            _conn.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0")
        _conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL)""")
        _conn.execute("""CREATE TABLE IF NOT EXISTS user_keys (
            user_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            ciphertext BLOB NOT NULL,
            last4 TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (user_id, provider))""")
        _conn.execute("""CREATE TABLE IF NOT EXISTS usage (
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            tokens INTEGER NOT NULL DEFAULT 0,
            requests INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, day))""")
        _conn.execute("""CREATE TABLE IF NOT EXISTS invite_codes (
            code TEXT PRIMARY KEY,
            created_at REAL NOT NULL,
            used_by INTEGER,
            used_at REAL)""")
        _conn.execute("""CREATE TABLE IF NOT EXISTS password_resets (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            used INTEGER NOT NULL DEFAULT 0)""")
        _conn.execute("""CREATE TABLE IF NOT EXISTS user_store (
            user_id INTEGER PRIMARY KEY,
            data TEXT NOT NULL,
            updated_at REAL NOT NULL)""")
        _conn.execute("""CREATE TABLE IF NOT EXISTS email_verifications (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            used INTEGER NOT NULL DEFAULT 0)""")
        _conn.execute("""CREATE TABLE IF NOT EXISTS published_pages (
            slug TEXT PRIMARY KEY,
            html TEXT NOT NULL,
            user_id INTEGER,
            conv_id TEXT,
            created_at REAL NOT NULL)""")
        # sessions' own primary key is token_hash (one row per login); every
        # sign-out-everywhere, password change, and account deletion instead
        # looks sessions up by user_id, which was an unindexed full scan.
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
        _conn.commit()
    return _conn


def init_db():
    with _lock:
        _connect()


# ---- password hashing (PBKDF2, stdlib) ----
def _hash_password(password: str, salt: bytes = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERS)
    return f"pbkdf2_sha256${_PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


# ---- users ----
class UserError(ValueError):
    pass


def create_user(email: str, password: str) -> dict:
    email = _norm_email(email)
    if "@" not in email or "." not in email.split("@")[-1]:
        raise UserError("enter a valid email address")
    if len(password or "") < 8:
        raise UserError("password must be at least 8 characters")
    with _lock:
        conn = _connect()
        if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise UserError("an account with that email already exists")
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?,?,?)",
            (email, _hash_password(password), time.time()))
        conn.commit()
        return {"id": cur.lastrowid, "email": email, "email_verified": False}


def _delete_user(user_id: int):
    """Only used to roll back a signup whose invite code failed to redeem —
    for a user-initiated account deletion see delete_account()."""
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()


def delete_account(user_id: int):
    """Permanently deletes a user and everything scoped to them: sessions,
    BYO keys, usage history, synced conversations, and outstanding
    reset/verification tokens. Deliberately leaves invite_codes.used_by and
    reqlog's requests.user_id pointing at the now-gone id (same as any other
    audit/usage log — see reqlog.py's migration notes) rather than nulling
    them out, which would let a spent invite code be redeemed a second time."""
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_keys WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM usage WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_store WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM password_resets WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM email_verifications WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()


def verify_user(email: str, password: str) -> dict | None:
    email = _norm_email(email)
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT id, email, password_hash, email_verified FROM users WHERE email = ?", (email,)).fetchone()
    if not row or not _verify_password(password, row[2]):
        return None
    return {"id": row[0], "email": row[1], "email_verified": bool(row[3])}


def verify_user_password(user_id: int, password: str) -> bool:
    """Re-checks a password for an already-authenticated user — used before
    letting them change their password or delete their account, so a hijacked
    session (or a stray CSRF) can't do either without knowing the password."""
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row) and _verify_password(password, row[0])


def get_user_by_email(email: str) -> dict | None:
    email = _norm_email(email)
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT id, email FROM users WHERE email = ?", (email,)).fetchone()
    return {"id": row[0], "email": row[1]} if row else None


def is_email_verified(user_id: int) -> bool:
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT email_verified FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row and row[0])


def set_password(user_id: int, new_password: str):
    if len(new_password or "") < 8:
        raise UserError("password must be at least 8 characters")
    with _lock:
        conn = _connect()
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                    (_hash_password(new_password), user_id))
        conn.commit()


# ---- sessions ----
def _tok_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (_tok_hash(token), user_id, now, now + _SESSION_TTL_S))
        conn.commit()
    return token


def user_for_session(token: str) -> dict | None:
    if not token:
        return None
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT s.user_id, s.expires_at, u.email FROM sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
            (_tok_hash(token),)).fetchone()
        if not row:
            return None
        if row[1] < time.time():
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_tok_hash(token),))
            conn.commit()
            return None
    return {"id": row[0], "email": row[2]}


def delete_session(token: str):
    if not token:
        return
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_tok_hash(token),))
        conn.commit()


def delete_other_sessions(user_id: int, keep_token: str | None):
    """Signs out every session for this user except (optionally) the caller's
    own — used after a password change, so other devices are kicked off
    without also logging the person out of the session they just used."""
    with _lock:
        conn = _connect()
        if keep_token:
            conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                        (user_id, _tok_hash(keep_token)))
        else:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.commit()


def delete_all_sessions(user_id: int):
    """Every session for this user, including the caller's own — "sign out
    everywhere" and account deletion both mean this literally."""
    delete_other_sessions(user_id, None)


# ---- bring-your-own provider keys (Phase 2) ----
class KeysDisabledError(RuntimeError):
    """Raised when APP_ENCRYPTION_KEY isn't configured — BYO keys are refused
    rather than ever stored unencrypted."""


_fernet_cache = None


def _fernet():
    global _fernet_cache
    if _fernet_cache is not None:
        return _fernet_cache
    if not config.APP_ENCRYPTION_KEY:
        raise KeysDisabledError(
            "bring-your-own-key storage is disabled on this server "
            "(APP_ENCRYPTION_KEY is not set)")
    from cryptography.fernet import Fernet   # imported lazily: optional at runtime
    try:
        _fernet_cache = Fernet(config.APP_ENCRYPTION_KEY.encode())
    except Exception as exc:
        raise KeysDisabledError(f"APP_ENCRYPTION_KEY is invalid: {exc}") from exc
    return _fernet_cache


def keys_enabled() -> bool:
    return bool(config.APP_ENCRYPTION_KEY)


def set_user_key(user_id: int, provider: str, api_key: str):
    api_key = (api_key or "").strip()
    if not api_key:
        raise UserError("API key can't be empty")
    ciphertext = _fernet().encrypt(api_key.encode())
    last4 = api_key[-4:] if len(api_key) >= 4 else api_key
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO user_keys (user_id, provider, ciphertext, last4, created_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(user_id, provider) DO UPDATE SET "
            "ciphertext=excluded.ciphertext, last4=excluded.last4, created_at=excluded.created_at",
            (user_id, provider, ciphertext, last4, time.time()))
        conn.commit()


def get_user_key(user_id: int, provider: str) -> str | None:
    """Decrypted key for one provider, or None if the user hasn't added one."""
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT ciphertext FROM user_keys WHERE user_id = ? AND provider = ?",
            (user_id, provider)).fetchone()
    if not row:
        return None
    try:
        return _fernet().decrypt(row[0]).decode()
    except Exception:
        return None   # corrupted/undecryptable — treat as absent, don't crash a request


def list_user_keys(user_id: int) -> list[dict]:
    """[{provider, last4}] — never the actual key."""
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT provider, last4 FROM user_keys WHERE user_id = ? ORDER BY provider",
            (user_id,)).fetchall()
    return [{"provider": r[0], "last4": r[1]} for r in rows]


def delete_user_key(user_id: int, provider: str):
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM user_keys WHERE user_id = ? AND provider = ?", (user_id, provider))
        conn.commit()


# ---- free-tier daily usage (Phase 3) ----
def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())   # UTC day — matches the "resets at UTC midnight" doc


def usage_today(user_id: int) -> dict:
    """This user's app-owned-key usage so far today (BYO-key calls never
    reach here — see main.py's quota check)."""
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT tokens, requests FROM usage WHERE user_id = ? AND day = ?",
                           (user_id, _today())).fetchone()
    return {"tokens": row[0] if row else 0, "requests": row[1] if row else 0}


def record_usage(user_id: int, tokens: int):
    """Add to today's tally. Called only after a real (non-cached, app-key)
    upstream call, so cache hits and BYO-key calls never count against it."""
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO usage (user_id, day, tokens, requests) VALUES (?,?,?,1) "
            "ON CONFLICT(user_id, day) DO UPDATE SET "
            "tokens = tokens + excluded.tokens, requests = requests + 1",
            (user_id, _today(), max(0, int(tokens or 0))))
        conn.commit()


# ---- invite-gated signup (Phase 4) ----
def create_invite_code() -> str:
    """Generate a fresh, unused invite code. Called from scripts/create_invite.py
    — there's deliberately no HTTP endpoint for this (don't add attack surface
    for something an operator runs offline)."""
    code = secrets.token_urlsafe(9)   # short-ish, still unguessable
    with _lock:
        conn = _connect()
        conn.execute("INSERT INTO invite_codes (code, created_at) VALUES (?,?)",
                    (code, time.time()))
        conn.commit()
    return code


def redeem_invite_code(code: str, user_id: int) -> bool:
    """Atomically mark a code used-by this user. False if it doesn't exist or
    was already redeemed — callers should treat that as a signup rejection."""
    code = (code or "").strip()
    if not code:
        return False
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "UPDATE invite_codes SET used_by = ?, used_at = ? "
            "WHERE code = ? AND used_by IS NULL",
            (user_id, time.time(), code))
        conn.commit()
        return cur.rowcount > 0


# ---- password reset via email (Phase 4) ----
_RESET_TTL_S = 60 * 60  # 1 hour


def create_reset_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO password_resets (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (_tok_hash(token), user_id, now, now + _RESET_TTL_S))
        conn.commit()
    return token


def user_id_for_reset_token(token: str) -> int | None:
    """The user this (unexpired, unused) reset token belongs to, or None."""
    if not token:
        return None
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT user_id, expires_at, used FROM password_resets WHERE token_hash = ?",
            (_tok_hash(token),)).fetchone()
    if not row or row[2] or row[1] < time.time():
        return None
    return row[0]


def consume_reset_token(token: str):
    """Mark a reset token used (single-use) and drop all of that user's live
    sessions — a password reset should log out anyone else holding a session."""
    user_id = user_id_for_reset_token(token)
    if user_id is None:
        return
    with _lock:
        conn = _connect()
        conn.execute("UPDATE password_resets SET used = 1 WHERE token_hash = ?", (_tok_hash(token),))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.commit()


# ---- email verification ----
_VERIFY_TTL_S = 60 * 60 * 24  # 24 hours


def create_verification_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO email_verifications (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (_tok_hash(token), user_id, now, now + _VERIFY_TTL_S))
        conn.commit()
    return token


def verify_email_token(token: str) -> int | None:
    """Marks the token used and the owning user's email verified in one
    transaction. Returns the user_id on success, None if the token is
    missing, expired, or already used."""
    if not token:
        return None
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT user_id, expires_at, used FROM email_verifications WHERE token_hash = ?",
            (_tok_hash(token),)).fetchone()
        if not row or row[2] or row[1] < time.time():
            return None
        conn.execute("UPDATE email_verifications SET used = 1 WHERE token_hash = ?", (_tok_hash(token),))
        conn.execute("UPDATE users SET email_verified = 1 WHERE id = ?", (row[0],))
        conn.commit()
        return row[0]


# ---- server-side conversation sync (Phase 4) ----
def get_user_store(user_id: int) -> str | None:
    """The user's saved chat-store JSON blob, or None if they've never synced."""
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT data FROM user_store WHERE user_id = ?", (user_id,)).fetchone()
    return row[0] if row else None


def set_user_store(user_id: int, data: str):
    if len(data or "") > 10_000_000:   # 10 MB — generous, but not unbounded
        raise UserError("conversation history too large to sync")
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO user_store (user_id, data, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
            (user_id, data, time.time()))
        conn.commit()


# ---- publish a generated project to a stable link on this server ----
_MAX_PUBLISH_BYTES = 3_000_000   # 3 MB — a generated single-page app fits comfortably under this


def create_published_page(html: str, user_id: int | None, conv_id: str | None) -> str:
    """Stores a stitched project's HTML under a short random slug and returns
    it. No expiry — this is meant to be a stable, shareable link, the same
    contract as a real deploy would give you."""
    if not (html or "").strip():
        raise UserError("nothing to publish")
    if len(html.encode("utf-8")) > _MAX_PUBLISH_BYTES:
        raise UserError("project is too large to publish (over 3 MB)")
    slug = secrets.token_urlsafe(6)
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO published_pages (slug, html, user_id, conv_id, created_at) VALUES (?,?,?,?,?)",
            (slug, html, user_id, conv_id, time.time()))
        conn.commit()
    return slug


def get_published_page(slug: str) -> str | None:
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT html FROM published_pages WHERE slug = ?", (slug,)).fetchone()
    return row[0] if row else None
