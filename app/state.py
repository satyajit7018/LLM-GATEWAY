"""Runtime state and auth-session helpers shared across routers.

Split out of the old monolithic main.py so app/routers/*.py can each depend
on the cache instances, request counters, and "who's logged in" helpers
without importing each other or reaching back into main.py (which would
create a circular import, since main.py is what wires the routers in).
"""
import threading

from fastapi import HTTPException, Request, Response

from . import config, store
from .cache import Cache
from .semantic_cache import SemanticCache

_cache = Cache()
_semantic = SemanticCache(persist=True)
_stats = {"hits_exact": 0, "hits_semantic": 0, "misses": 0, "errors": 0,
          "rate_limited": 0, "circuit_open": 0}
_stats_lock = threading.Lock()


def _bump(key: str):
    with _stats_lock:
        _stats[key] += 1


def _cost(tokens) -> float:
    return (tokens or 0) / 1000 * config.COST_PER_1K_TOKENS


# Loopback-only guard shared by local code execution (routers/workspace.py)
# and the admin reset endpoint (routers/admin.py) — neither should be
# reachable from beyond this machine by default.
_LOOPBACK = {"127.0.0.1", "::1", "localhost", None}


# --- accounts / auth session ------------------------------------------
_COOKIE = "session"


def current_user(request: Request):
    """The logged-in user (or None). Reads the session cookie."""
    return store.user_for_session(request.cookies.get(_COOKIE))


def require_user(request: Request):
    """Dependency: 401 unless a user is logged in. No-op when AUTH_REQUIRED is
    off (used by the test suite, which exercises the shared gateway, not auth) —
    fine for /generate, where the caller's identity doesn't change the request.
    Endpoints that read/write *this user's own data* (keys, later usage) must
    use require_real_user instead, which always enforces a real session."""
    if not config.AUTH_REQUIRED:
        return current_user(request)
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="sign in to continue")
    return user


def require_real_user(request: Request):
    """Dependency: always 401s without a real session, regardless of
    AUTH_REQUIRED — for endpoints that operate on one user's own data and so
    can't meaningfully run for "no one"."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="sign in to continue")
    return user


def _set_session_cookie(resp: Response, token: str):
    resp.set_cookie(_COOKIE, token, max_age=60 * 60 * 24 * 30, httponly=True,
                    samesite="lax", secure=config.COOKIE_SECURE, path="/")
