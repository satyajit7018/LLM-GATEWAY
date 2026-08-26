"""FastAPI gateway (Steps 2-7).

POST /generate  -> exact-match + semantic cached LLM call, behind a rate limiter,
                   retry/backoff and a circuit breaker.
GET  /stats     -> live counters + backend info.
GET  /metrics   -> Prometheus exposition.
GET  /healthz   -> liveness.

`POST /generate?cache_mode=none|exact|semantic` overrides caching per request so
the load test (Step 6) can compare configurations against one running server.
"""
import json
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, mailer, metrics, reqlog, runner, store
from .cache import Cache, request_signature
from .files import extract_pdf_text
from .llm import LLMError, close_client, generate, generate_stream, last_quota, validate_key
from .resilience import (
    CircuitOpenError,
    auth_bucket,
    breaker,
    bucket,
    per_user_bucket,
    resilient_generate,
)
from .semantic_cache import SemanticCache

@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init_db()   # ensure the users/sessions tables exist
    yield
    # On shutdown: kill any local runs still in flight so their child
    # processes don't orphan after the server stops, and release the
    # pooled LLM HTTP client's connections.
    runner.shutdown()
    close_client()


app = FastAPI(title="LLM Gateway", version="0.5.0", lifespan=lifespan)

_cache = Cache()
_semantic = SemanticCache()
_stats = {"hits_exact": 0, "hits_semantic": 0, "misses": 0, "errors": 0,
          "rate_limited": 0, "circuit_open": 0}
_stats_lock = threading.Lock()


def _bump(key: str):
    with _stats_lock:
        _stats[key] += 1


def _cost(tokens) -> float:
    return (tokens or 0) / 1000 * config.COST_PER_1K_TOKENS


# Injected as a system turn when a request has code_mode on. Steers the model
# to a code-first answer, which the UI then syntax-highlights / previews.
CODE_PREAMBLE = (
    "You are a coding assistant. Prioritize correct, runnable code. Lead your "
    "answer with the code inside a fenced block whose info string is the "
    "language (```python, ```js, ```html, …). Keep prose short and put it after "
    "the code. If the request is a web UI or something visual, return one "
    "self-contained ```html block (inline CSS/JS, no external files) so it can "
    "be previewed directly. If the user attached an image, treat it as a "
    "design reference — a screenshot, mockup, or wireframe of a UI to build or "
    "match — and write HTML/CSS (and JS if needed) that reproduces its layout, "
    "spacing, colors, and text as closely as you can, even if they didn't "
    "spell out every detail in words."
)


def _apply_code_mode(hist: list[dict], code_mode: bool) -> list[dict]:
    """Prepend the code-first system turn when code_mode is on."""
    if code_mode:
        return [{"role": "system", "content": CODE_PREAMBLE}] + hist
    return hist


def _normalize_attachments(attachments) -> list[dict]:
    """Return uniform {name, text} dicts, extracting PDF text server-side."""
    out = []
    for a in attachments:
        if a.pdf_b64:
            try:
                text = extract_pdf_text(a.pdf_b64)
            except Exception as exc:  # malformed/encrypted PDF
                text = f"[could not read PDF {a.name}: {exc}]"
            out.append({"name": a.name, "text": text})
        else:
            out.append({"name": a.name, "text": a.text})
    return out


def _uid(user):
    """This user's id for attributing a request-log row, or None (anonymous /
    AUTH_REQUIRED off) — keeps per-user usage/cost views from mixing accounts."""
    return user["id"] if user else None


def _guard_auth_rate(request: Request):
    """Security review finding: login/signup/forgot-password/resend had no
    throttling at all. Keyed by client IP (there's no user yet at login
    time) — a shared bucket across these four endpoints, so someone can't
    dodge the limit by bouncing between them."""
    ip = request.client.host if request.client else "unknown"
    if not auth_bucket.allow(f"auth:{ip}"):
        raise HTTPException(status_code=429, detail="too many attempts — wait a moment and try again")


def _rate_limit_key(request: Request, user) -> str:
    """Per-account rate-limit key — falls back to client IP when nobody's
    logged in (AUTH_REQUIRED off), so anonymous access still gets *a* per-key
    limit rather than being exempt from the per-user check entirely."""
    if user:
        return f"user:{user['id']}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


def _byo_key_for(user, model_id: str):
    """The logged-in user's own key for this model's provider, if they've
    added one (hybrid build, Phase 2) — None falls back to the app's key."""
    if not user:
        return None
    entry = config.MODEL_BY_ID.get(model_id)
    if not entry:
        return None
    try:
        return store.get_user_key(user["id"], entry["provider"])
    except store.KeysDisabledError:
        return None


def _check_free_quota(user, byo_key):
    """429 if this call would use the app's own key and the user has already
    used up today's free allowance (hybrid build, Phase 3). A no-op for BYO-key
    calls (costs the user, not us), when quota is disabled (limit=0), or when
    there's no user to attribute usage to (AUTH_REQUIRED off)."""
    if byo_key or not user or not config.FREE_DAILY_TOKEN_LIMIT:
        return
    used = store.usage_today(user["id"])["tokens"]
    if used >= config.FREE_DAILY_TOKEN_LIMIT:
        raise HTTPException(status_code=429,
            detail=f"Free daily limit reached ({config.FREE_DAILY_TOKEN_LIMIT} tokens). "
                   "Add your own API key in Settings to keep going, or come back tomorrow.")


def _charge_free_quota(user, byo_key, tokens):
    """Record usage against today's free allowance — only for real (uncached)
    calls that used the app's own key."""
    if byo_key or not user:
        return
    store.record_usage(user["id"], tokens)


class Attachment(BaseModel):
    name: str
    text: str = ""            # text content read client-side (text/code files)
    pdf_b64: Optional[str] = None  # base64 PDF; text extracted server-side


class Turn(BaseModel):
    role: str                 # "user" | "assistant"
    content: str


class GenerateRequest(BaseModel):
    prompt: str = Field(default="", max_length=200_000)
    images: list[str] = Field(default_factory=list)       # data: URLs
    attachments: list[Attachment] = Field(default_factory=list)
    model: Optional[str] = None                           # catalog id; None = default
    history: list[Turn] = Field(default_factory=list)     # prior turns (memory mode)
    code_mode: bool = False                               # code-first assistant persona

    def has_payload(self) -> bool:
        return bool(self.prompt.strip() or self.images or self.attachments)

    def model_id(self) -> str:
        return self.model if self.model in config.MODEL_BY_ID else config.DEFAULT_MODEL_ID


class GenerateResponse(BaseModel):
    text: str
    model: str
    tokens: int
    cached: bool
    cache_type: str  # "none" | "exact" | "semantic"
    latency_ms: float
    match_score: Optional[float] = None


_STATIC = Path(__file__).parent / "static"
_INDEX = _STATIC / "index.html"

# Vendored front-end assets (Monaco editor, etc.) served locally so the app
# stays fully offline — no CDN dependency. Long cache: these are versioned files.
_VENDOR = _STATIC / "vendor"
if _VENDOR.is_dir():
    app.mount("/vendor", StaticFiles(directory=str(_VENDOR)), name="vendor")


@app.get("/")
def index():
    """Serve the single-page front end (no-cache so UI updates show immediately)."""
    return FileResponse(_INDEX, headers={"Cache-Control": "no-cache"})


# --- publish a Code-tab project to a stable link on this server ----------
# The self-hosted equivalent of a one-click deploy: no third-party account or
# API token needed. The link works today on localhost and would work
# publicly the moment this app itself is hosted somewhere.
class PublishRequest(BaseModel):
    html: str = Field(max_length=3_500_000)
    conv_id: Optional[str] = Field(default=None, max_length=64)


@app.post("/publish")
def publish_page(body: PublishRequest, request: Request):
    user = current_user(request)
    try:
        slug = store.create_published_page(body.html, user["id"] if user else None, body.conv_id)
    except store.UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"slug": slug, "url": f"{config.PUBLIC_BASE_URL}/p/{slug}"}


@app.get("/p/{slug}")
def published_page(slug: str):
    """Serves a published project as a standalone page — not wrapped in the
    app shell, so it's a real page you can open, bookmark, or share.

    Security review finding: this serves arbitrary user-authored HTML/JS at
    the app's own origin, and /publish doesn't require being signed in — so
    a malicious page here could otherwise ride an already-logged-in
    visitor's session cookie (same-origin fetch/XHR isn't blocked by
    SameSite) to call the app's own API as them. `connect-src 'none'` and
    `form-action 'none'` block exactly that — the page's own inline
    JS/interactivity still runs, it just can't phone home to this app or
    submit a form anywhere.
    """
    html = store.get_published_page(slug)
    if html is None:
        raise HTTPException(status_code=404, detail="that page doesn't exist, or was never published")
    return HTMLResponse(html, headers={
        "Content-Security-Policy": "connect-src 'none'; form-action 'none'; frame-ancestors 'none'",
    })


# --- accounts / auth (Phase 1) -------------------------------------------
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


class Credentials(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=1024)
    invite_code: str = Field(default="", max_length=64)   # only checked on signup


def _set_session_cookie(resp: Response, token: str):
    resp.set_cookie(_COOKIE, token, max_age=60 * 60 * 24 * 30, httponly=True,
                    samesite="lax", secure=config.COOKIE_SECURE, path="/")


@app.post("/auth/signup")
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


def _send_verification_email(user: dict):
    """Best-effort — signup must still succeed even if mail delivery is down
    or misconfigured; the user can always hit /auth/resend-verification."""
    token = store.create_verification_token(user["id"])
    link = f"{config.PUBLIC_BASE_URL}/?verify_token={token}"
    try:
        mailer.send_verification_email(user["email"], link)
    except Exception:
        pass


@app.post("/auth/login")
def auth_login(creds: Credentials, response: Response, request: Request):
    _guard_auth_rate(request)
    user = store.verify_user(creds.email, creds.password)
    if not user:
        raise HTTPException(status_code=401, detail="wrong email or password")
    _set_session_cookie(response, store.create_session(user["id"]))
    return {"user": user}


@app.post("/auth/logout")
def auth_logout(request: Request, response: Response):
    store.delete_session(request.cookies.get(_COOKIE))
    response.delete_cookie(_COOKIE, path="/")
    return {"ok": True}


@app.get("/auth/me")
def auth_me(request: Request):
    user = current_user(request)
    if user:
        user = {**user, "email_verified": store.is_email_verified(user["id"])}
    return {"user": user, "auth_required": config.AUTH_REQUIRED,
            "signup_requires_invite": config.SIGNUP_REQUIRES_INVITE}


class VerifyEmail(BaseModel):
    token: str = Field(max_length=256)


@app.post("/auth/verify-email")
def auth_verify_email(body: VerifyEmail):
    if store.verify_email_token(body.token) is None:
        raise HTTPException(status_code=400, detail="that verification link is invalid or has expired")
    return {"ok": True}


@app.post("/auth/resend-verification")
def auth_resend_verification(request: Request, user=Depends(require_real_user)):
    _guard_auth_rate(request)
    if store.is_email_verified(user["id"]):
        return {"ok": True, "already_verified": True}
    _send_verification_email(user)
    return {"ok": True, "already_verified": False}


class ChangePassword(BaseModel):
    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)


@app.post("/auth/change-password")
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


@app.post("/auth/sign-out-everywhere")
def auth_sign_out_everywhere(response: Response, user=Depends(require_real_user)):
    store.delete_all_sessions(user["id"])
    response.delete_cookie(_COOKIE, path="/")   # this device too — "everywhere" means everywhere
    return {"ok": True}


class DeleteAccount(BaseModel):
    password: str = Field(max_length=1024)


@app.post("/auth/delete-account")
def auth_delete_account(body: DeleteAccount, response: Response, user=Depends(require_real_user)):
    if not store.verify_user_password(user["id"], body.password):
        raise HTTPException(status_code=400, detail="incorrect password")
    store.delete_account(user["id"])
    response.delete_cookie(_COOKIE, path="/")
    return {"ok": True}


class ForgotPassword(BaseModel):
    email: str = Field(max_length=254)


@app.post("/auth/forgot-password")
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


@app.post("/auth/reset-password")
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


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/stats")
def stats():
    with _stats_lock:
        s = dict(_stats)
    hits = s["hits_exact"] + s["hits_semantic"]
    total = hits + s["misses"]
    s["hit_rate"] = round(hits / total, 4) if total else 0.0
    s["cache_backend"] = _cache.backend_name
    s["semantic_backend"] = _semantic.backend_name
    s["semantic_entries"] = _semantic.size
    s["llm_backend"] = config.LLM_BACKEND
    s["circuit_state"] = breaker.state
    return s


@app.post("/admin/reset")
def admin_reset(request: Request):
    """Clear both caches and counters so a load-test pass starts cold + fair.

    Security review finding: this was public and unauthenticated — anyone
    could wipe the shared cache/metrics with one POST. Restricted to
    loopback the same way local code execution is (see _guard_run below);
    this is a local dev/load-testing convenience, not something that should
    be reachable from the open internet.
    """
    host = request.client.host if request.client else None
    if host not in _LOOPBACK:
        raise HTTPException(403, "restricted to this machine")
    _cache.clear()
    _semantic.clear()
    with _stats_lock:
        for k in _stats:
            _stats[k] = 0
    return {"status": "reset"}


@app.get("/models")
def models(request: Request):
    """Catalog of selectable models. A model is available if the app has a key
    for its provider (the free tier) or, once logged in, if the user has added
    their own key for it (hybrid build, Phase 2) — 'source' says which."""
    user = current_user(request)
    user_providers = set()
    if user:
        try:
            user_providers = {k["provider"] for k in store.list_user_keys(user["id"])}
        except store.KeysDisabledError:
            pass
    out = []
    for m in config.MODEL_CATALOG:
        has_app_key = bool(config.provider_key(m["provider"])) or config.LLM_BACKEND == "mock"
        has_user_key = m["provider"] in user_providers
        out.append({
            "id": m["id"], "label": m["label"], "provider": m["provider"],
            "best_for": m.get("best_for", ""), "vision": m.get("vision", False),
            "available": has_app_key or has_user_key,
            "source": "user" if has_user_key else ("app" if has_app_key else None),
        })
    return {"default": config.DEFAULT_MODEL_ID, "backend": config.LLM_BACKEND,
            "models": out, "keys_enabled": store.keys_enabled()}


# --- bring-your-own provider keys (hybrid build, Phase 2) -----------------
class KeyIn(BaseModel):
    provider: str = Field(max_length=32)
    api_key: str = Field(max_length=512)


@app.get("/keys")
def list_keys(user=Depends(require_real_user)):
    """This user's connected providers — never the actual key, just provider +
    last 4 characters, so the UI can show 'sk-…ab12' without re-exposing it."""
    if not store.keys_enabled():
        return {"enabled": False, "keys": []}
    return {"enabled": True, "keys": store.list_user_keys(user["id"])}


@app.post("/keys")
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


@app.delete("/keys/{provider}")
def remove_key(provider: str, user=Depends(require_real_user)):
    store.delete_user_key(user["id"], provider)
    return {"ok": True}


# --- local code execution (real interpreters on this machine) ------------
class RunFile(BaseModel):
    name: str = Field(max_length=256)
    content: str = Field(default="", max_length=2_000_000)


class RunRequest(BaseModel):
    files: list[RunFile] = Field(default_factory=list)
    entry: str = Field(default="", max_length=256)
    timeout: int = Field(default=300, ge=1, le=1800)
    install: bool = False
    packages: list[str] = Field(default_factory=list)
    workspace: Optional[str] = None
    command: Optional[str] = Field(default=None, max_length=4000)


class RunInput(BaseModel):
    session: str = Field(max_length=64)
    text: str = Field(default="", max_length=100_000)


_LOOPBACK = {"127.0.0.1", "::1", "localhost", None}


def _guard_run(request: Request):
    """Local execution / file access is loopback-only unless explicitly opened,
    and may require a shared token. Blocks remote code execution by default."""
    if not config.ENABLE_LOCAL_RUN:
        raise HTTPException(403, "Local run is disabled (set ENABLE_LOCAL_RUN=1)")
    host = request.client.host if request.client else None
    if not config.RUN_ALLOW_REMOTE and host not in _LOOPBACK:
        raise HTTPException(403, "Local run is restricted to this machine (set RUN_ALLOW_REMOTE=1 to override behind your own auth)")
    if config.RUN_TOKEN and request.headers.get("x-run-token") != config.RUN_TOKEN:
        raise HTTPException(401, "missing or invalid X-Run-Token")


@app.get("/run/env")
def run_env():
    """Which language runtimes are installed on this machine."""
    return {"enabled": config.ENABLE_LOCAL_RUN, "languages": runner.available_languages()}


@app.post("/run/start")
def run_start(req: RunRequest, request: Request):
    """Start an interactive local run; returns a session id to stream + feed stdin.
    With `command`, runs that shell command in the workspace instead of a file."""
    _guard_run(request)
    if not req.entry and not req.command:
        raise HTTPException(400, "no entry file or command")
    files = [f.model_dump() for f in req.files]
    sid = runner.start_run(files, req.entry, install=req.install, packages=req.packages,
                           timeout=req.timeout, workspace=req.workspace, command=req.command)
    return {"session": sid}


@app.get("/run/stream/{sid}")
def run_stream(sid: str, request: Request):
    """Stream a run session's output as SSE until the process exits."""
    _guard_run(request)
    def gen():
        for channel, text in runner.stream_run(sid):
            yield f"data: {json.dumps({'channel': channel, 'text': text})}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/run/input")
def run_input(req: RunInput, request: Request):
    """Send a line to a running process's stdin (interactive input)."""
    _guard_run(request)
    return {"ok": runner.send_input(req.session, req.text)}


@app.post("/run/kill")
def run_kill(req: RunInput, request: Request):
    _guard_run(request)
    return {"ok": runner.kill_run(req.session)}


@app.get("/workspace/list")
def workspace_list(request: Request, ws: str):
    """Files a run created or kept in a persistent workspace."""
    _guard_run(request)
    return {"workspace": ws, "files": runner.list_workspace(ws)}


@app.get("/workspace/file")
def workspace_file(request: Request, ws: str, path: str, download: int = 0):
    """Return one workspace file (inline for viewing, or as a download)."""
    _guard_run(request)
    full = runner.workspace_file(ws, path)
    if not full:
        raise HTTPException(404, "no such file")
    name = path.rsplit("/", 1)[-1]
    disp = "attachment" if download else "inline"
    return FileResponse(full, filename=name,
                        headers={"Content-Disposition": f'{disp}; filename="{name}"'})


class GitCommit(BaseModel):
    ws: str = Field(max_length=64)
    message: str = Field(default="", max_length=200)


@app.post("/workspace/commit")
def workspace_commit(req: GitCommit, request: Request):
    """Git-snapshot a workspace's files."""
    _guard_run(request)
    return runner.git_commit(req.ws, req.message)


@app.get("/workspace/history")
def workspace_history(request: Request, ws: str):
    _guard_run(request)
    return {"history": runner.git_history(ws)}


class GitRestore(BaseModel):
    ws: str = Field(max_length=64)
    ref: str = Field(max_length=64)


@app.post("/workspace/restore")
def workspace_restore(req: GitRestore, request: Request):
    """Restore a workspace's files to a past snapshot."""
    _guard_run(request)
    return runner.git_restore(req.ws, req.ref)


def _guard_lsp(request: Request):
    """Python IntelliSense (Jedi static analysis — no code execution) is still
    loopback-only by default, mirroring the run guard, minus the run toggle."""
    host = request.client.host if request.client else None
    if not config.RUN_ALLOW_REMOTE and host not in _LOOPBACK:
        raise HTTPException(403, "language features are restricted to this machine")
    if config.RUN_TOKEN and request.headers.get("x-run-token") != config.RUN_TOKEN:
        raise HTTPException(401, "missing or invalid X-Run-Token")


class LspRequest(BaseModel):
    source: str = Field(default="", max_length=1_000_000)
    line: int = Field(ge=1)
    column: int = Field(ge=1)
    kind: str = Field(default="complete")  # complete | hover | definition


@app.post("/lsp/python")
def lsp_python(req: LspRequest, request: Request):
    """Real Python intelligence via Jedi (completions / hover / go-to-definition).
    Static analysis only — it never runs the code."""
    _guard_lsp(request)
    try:
        import jedi
    except Exception:
        return {"ok": False, "error": "jedi not installed (pip install jedi)"}
    col = max(0, req.column - 1)  # Monaco columns are 1-based, Jedi's are 0-based
    try:
        script = jedi.Script(code=req.source)
        if req.kind == "hover":
            docs = [n.docstring() for n in script.help(req.line, col) if n.docstring()]
            return {"ok": True, "hover": docs[0] if docs else ""}
        if req.kind == "definition":
            defs = script.goto(req.line, col, follow_imports=True)
            out = [{"line": d.line, "column": (d.column or 0) + 1, "name": d.name}
                   for d in defs if d.line]
            return {"ok": True, "definitions": out}
        comps = script.complete(req.line, col)
        items = [{"label": c.name, "insert": c.name, "kind": c.type or "text"}
                 for c in comps[:80]]
        return {"ok": True, "completions": items}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


@app.get("/quota")
def quota(model: str = None):
    """Latest rate-limit snapshot for the selected model's provider.

    `model` is a catalog id (e.g. "gemini/flash"); its provider decides which
    quota is shown, so switching models switches the numbers. Empty until the
    first real call to that provider; not applicable to the mock backend.
    Some providers (e.g. Gemini) don't return token-quota headers.
    """
    entry = config.MODEL_BY_ID.get(model) or config.MODEL_BY_ID[config.DEFAULT_MODEL_ID]
    provider = entry["provider"]
    q = last_quota(provider)
    return {
        "backend": config.LLM_BACKEND,
        "provider": provider,
        "model": entry["id"],
        "model_label": entry.get("label", entry["id"]),
        "reports_quota": provider == "groq",  # Groq sends x-ratelimit-* headers
        "available": bool(q) and config.LLM_BACKEND != "mock",
        "quota": q,
    }


@app.get("/metrics")
def prometheus_metrics():
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)


@app.post("/generate", response_model=GenerateResponse)
def generate_endpoint(req: GenerateRequest, request: Request,
                      cache_mode: str = "default", user=Depends(require_user)):
    start = time.perf_counter()

    if not req.has_payload():
        raise HTTPException(status_code=422, detail="prompt, image or file required")

    # --- rate limit (Step 5) — global cap, then a per-account cap (hybrid build)
    # so one busy account can't 429 every other account by exhausting the
    # shared budget alone. ---
    if not bucket.allow():
        _bump("rate_limited")
        metrics.record_error("rate_limited")
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    if not per_user_bucket.allow(_rate_limit_key(request, user)):
        _bump("rate_limited")
        metrics.record_error("rate_limited")
        raise HTTPException(status_code=429, detail="you're sending requests too fast — slow down a bit")

    atts = _normalize_attachments(req.attachments)
    hist = _apply_code_mode([t.model_dump() for t in req.history], req.code_mode)
    has_extras = bool(req.images or atts or hist)
    model_id = req.model_id()
    sig = request_signature(req.prompt, req.images, atts, model_id, hist)

    use_exact = cache_mode in ("default", "exact", "semantic")
    # Semantic cache only applies to plain-text prompts on the default model with
    # no conversation history: embedding the prompt while ignoring an
    # image/file/model/history would false-hit.
    use_semantic = (
        (config.SEMANTIC_CACHE_ENABLED if cache_mode == "default"
         else cache_mode == "semantic")
        and not has_extras
        and model_id == config.DEFAULT_MODEL_ID
    )

    def _elapsed_ms():
        return round((time.perf_counter() - start) * 1000, 2)

    # --- exact-match cache (Step 3) — keyed on prompt + files + images ---
    if use_exact:
        cached = _cache.get(sig)
        if cached is not None:
            _bump("hits_exact")
            metrics.record_request("hit_exact", time.perf_counter() - start)
            reqlog.log("exact", "exact", cached["model"], cached["tokens"],
                       _elapsed_ms(), 0.0, bool(req.images), bool(atts), user_id=_uid(user))
            return GenerateResponse(**cached, cached=True, cache_type="exact",
                                    latency_ms=_elapsed_ms())

    # --- semantic cache (Step 4) ---
    if use_semantic:
        match = _semantic.lookup(req.prompt)
        if match is not None:
            response, _matched, score = match
            _bump("hits_semantic")
            metrics.record_request("hit_semantic", time.perf_counter() - start)
            reqlog.log("semantic", "semantic", response["model"],
                       response["tokens"], _elapsed_ms(), 0.0, user_id=_uid(user))
            return GenerateResponse(**response, cached=True, cache_type="semantic",
                                    latency_ms=_elapsed_ms(),
                                    match_score=round(score, 4))

    # --- upstream call behind circuit breaker + retry/backoff (Step 5) ---
    byo_key = _byo_key_for(user, model_id)
    _check_free_quota(user, byo_key)   # Phase 3: only bites app-key (free-tier) calls
    try:
        result = resilient_generate(
            lambda _p: generate(req.prompt, images=req.images, attachments=atts,
                                 model_id=model_id, history=hist, key_override=byo_key),
            req.prompt,
        )
    except CircuitOpenError as exc:
        _bump("circuit_open")
        metrics.record_error("circuit_open")
        raise HTTPException(status_code=503, detail=str(exc))
    except LLMError as exc:
        _bump("errors")
        metrics.record_error("upstream")
        raise HTTPException(status_code=502, detail=str(exc))

    if use_exact:
        _cache.set(sig, result)
    if use_semantic:
        _semantic.add(req.prompt, result)
    _charge_free_quota(user, byo_key, result["tokens"])

    _bump("misses")
    metrics.record_request("miss", time.perf_counter() - start)
    # Log the catalog id (not the raw provider model string) — it's the stable,
    # user-facing key the UI's per-model usage view groups by.
    reqlog.log("miss", "none", model_id, result["tokens"], _elapsed_ms(),
               _cost(result["tokens"]), bool(req.images), bool(atts), user_id=_uid(user))
    return GenerateResponse(**result, cached=False, cache_type="none",
                            latency_ms=_elapsed_ms())


@app.get("/log/summary")
def log_summary():
    """Aggregated request log (hit rate, tokens, cost by result)."""
    return reqlog.summary()


# Maps a provider's raw model string (e.g. "openai/gpt-oss-20b") back to its
# catalog id (e.g. "groq/gpt-oss-20b"). reqlog rows written before this
# mapping existed were logged under the raw string; folding them in here
# means old usage stays visible under the model's current catalog id instead
# of sitting orphaned under a key nothing looks up anymore. It also makes a
# future MODEL_CATALOG id rename safe, since the join key is the provider's
# model string, not the id.
_RAW_MODEL_TO_ID = {m["model"]: m["id"] for m in config.MODEL_CATALOG}


@app.get("/log/usage")
def log_usage(user=Depends(require_real_user)):
    """This user's own per-model token/request/cost totals from real
    (non-cached) calls, keyed by catalog id — durable across restarts and
    shared across the user's own browsers/devices. Scoped to the logged-in
    user (hybrid build) — this used to be a whole-server aggregate, which
    mixed every account's usage together once there was more than one."""
    merged: dict = {}
    for key, val in reqlog.usage_by_model(user_id=user["id"]).items():
        target = merged.setdefault(_RAW_MODEL_TO_ID.get(key, key),
                                    {"requests": 0, "tokens": 0, "cost_usd": 0.0})
        target["requests"] += val["requests"]
        target["tokens"] += val["tokens"]
        target["cost_usd"] = round(target["cost_usd"] + val["cost_usd"], 6)
    return {"by_model": merged}


@app.get("/usage/free")
def usage_free(user=Depends(require_real_user)):
    """This user's free-tier (app-owned-key) usage today, and the daily cap —
    hybrid build, Phase 3. BYO-key usage never counts here."""
    limit = config.FREE_DAILY_TOKEN_LIMIT
    used = store.usage_today(user["id"])["tokens"]
    return {"unlimited": limit == 0, "limit": limit, "used": used,
            "remaining": max(0, limit - used) if limit else None}


# --- server-side conversation sync (hybrid build, Phase 4) ----------------
class SyncPush(BaseModel):
    data: str = Field(max_length=10_000_000)   # the frontend's whole chat-store JSON, as a string


@app.get("/sync/store")
def sync_pull(user=Depends(require_real_user)):
    """This user's synced chat history, so it follows them across browsers/
    devices instead of being stuck in one browser's localStorage."""
    return {"data": store.get_user_store(user["id"])}


@app.put("/sync/store")
def sync_push(body: SyncPush, user=Depends(require_real_user)):
    try:
        store.set_user_store(user["id"], body.data)
    except store.UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@app.post("/generate/stream")
def generate_stream_endpoint(req: GenerateRequest, request: Request, cache_mode: str = "default",
                             user=Depends(require_user)):
    """SSE streaming variant of /generate.

    Cache hits are emitted instantly as a single frame; misses stream tokens
    from the provider as they arrive. Rate limiting + circuit breaking still
    apply; mid-stream retries do not (a partial stream can't be replayed), so
    the non-streaming /generate remains the fully-resilient path.
    """
    start = time.perf_counter()
    if not req.has_payload():
        raise HTTPException(status_code=422, detail="prompt, image or file required")
    if not bucket.allow():
        _bump("rate_limited")
        metrics.record_error("rate_limited")
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    if not per_user_bucket.allow(_rate_limit_key(request, user)):
        _bump("rate_limited")
        metrics.record_error("rate_limited")
        raise HTTPException(status_code=429, detail="you're sending requests too fast — slow down a bit")

    atts = _normalize_attachments(req.attachments)
    hist = _apply_code_mode([t.model_dump() for t in req.history], req.code_mode)
    has_extras = bool(req.images or atts or hist)
    model_id = req.model_id()
    byo_key = _byo_key_for(user, model_id)
    _check_free_quota(user, byo_key)   # before any SSE headers are sent, so this is a clean 429
    sig = request_signature(req.prompt, req.images, atts, model_id, hist)
    use_exact = cache_mode in ("default", "exact", "semantic")
    use_semantic = (
        (config.SEMANTIC_CACHE_ENABLED if cache_mode == "default"
         else cache_mode == "semantic") and not has_extras
        and model_id == config.DEFAULT_MODEL_ID
    )

    def frame(obj):
        return f"data: {json.dumps(obj)}\n\n"

    def elapsed_ms():
        return round((time.perf_counter() - start) * 1000, 2)

    def cached_frames(payload, cache_type, score=None):
        meta = {"done": True, "cached": True, "cache_type": cache_type,
                "text": payload["text"], "tokens": payload["tokens"],
                "model": payload["model"], "latency_ms": elapsed_ms(),
                "match_score": score}
        yield frame({"full": payload["text"]})
        yield frame(meta)
        yield "data: [DONE]\n\n"

    def sse():
        # Cache checks first — a hit streams instantly.
        if use_exact:
            hit = _cache.get(sig)
            if hit is not None:
                _bump("hits_exact")
                metrics.record_request("hit_exact", time.perf_counter() - start)
                reqlog.log("exact", "exact", hit["model"], hit["tokens"],
                           elapsed_ms(), 0.0, bool(req.images), bool(atts), True, _uid(user))
                yield from cached_frames(hit, "exact")
                return
        if use_semantic:
            match = _semantic.lookup(req.prompt)
            if match is not None:
                payload, _m, score = match
                _bump("hits_semantic")
                metrics.record_request("hit_semantic", time.perf_counter() - start)
                reqlog.log("semantic", "semantic", payload["model"],
                           payload["tokens"], elapsed_ms(), 0.0, streamed=True, user_id=_uid(user))
                yield from cached_frames(payload, "semantic", round(score, 4))
                return

        # Miss — stream from the provider behind the circuit breaker.
        try:
            breaker.before_call()
        except CircuitOpenError as exc:
            _bump("circuit_open")
            metrics.record_error("circuit_open")
            yield frame({"error": str(exc)})
            return
        result, streamed_any = None, False
        try:
            for ev in generate_stream(req.prompt, images=req.images,
                                      attachments=atts, model_id=model_id, history=hist,
                                      key_override=byo_key):
                if "delta" in ev:
                    streamed_any = True
                    yield frame({"delta": ev["delta"]})
                elif "final" in ev:
                    result = ev["final"]
        except LLMError as exc:
            # Some providers' streaming endpoints are flaky (e.g. Gemini's
            # OpenAI-compat layer 503s intermittently). If nothing streamed yet,
            # fall back to a single non-streaming call so the request succeeds.
            if not streamed_any:
                try:
                    result = generate(req.prompt, images=req.images,
                                      attachments=atts, model_id=model_id, history=hist,
                                      key_override=byo_key)
                    yield frame({"full": result["text"]})
                except LLMError as exc2:
                    breaker.record_failure()
                    _bump("errors")
                    metrics.record_error("upstream")
                    yield frame({"error": str(exc2)})
                    return
            else:
                breaker.record_failure()
                _bump("errors")
                metrics.record_error("upstream")
                yield frame({"error": str(exc)})
                return
        if result is None:  # stream ended without a result frame
            _bump("errors")
            metrics.record_error("upstream")
            yield frame({"error": "empty response from provider"})
            return
        breaker.record_success()

        if use_exact:
            _cache.set(sig, result)
        if use_semantic:
            _semantic.add(req.prompt, result)
        _charge_free_quota(user, byo_key, result["tokens"])
        _bump("misses")
        metrics.record_request("miss", time.perf_counter() - start)
        reqlog.log("miss", "none", model_id, result["tokens"],
                   elapsed_ms(), _cost(result["tokens"]),
                   bool(req.images), bool(atts), True, _uid(user))
        yield frame({"done": True, "cached": False, "cache_type": "none",
                     "tokens": result["tokens"], "model": result["model"],
                     "latency_ms": elapsed_ms(), "match_score": None})
        yield "data: [DONE]\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream")
