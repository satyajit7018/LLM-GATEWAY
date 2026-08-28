"""FastAPI gateway (Steps 2-7).

App wiring lives here: lifespan, static/vendor mounts, the index page, and
publish/publish-view. Everything else is split into app/routers/* by concern
(auth, workspace/local-run, the generate/cache/sync gateway, admin/metrics) —
see app/state.py for the shared cache/counter/session-helper singletons they
all depend on.
"""
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, runner, store
from .llm import close_client
from .routers import admin, auth, gateway, workspace
from .state import current_user

# Re-exported for backward compatibility: tests and any external code that
# imported these from app.main before the router split keep working
# unmodified.
from .state import _cache, _semantic, _stats, _stats_lock  # noqa: F401
from .routers.gateway import CODE_PREAMBLE, _apply_code_mode  # noqa: F401

__all__ = [
    "app",
    "_cache",
    "_semantic",
    "_stats",
    "_stats_lock",
    "CODE_PREAMBLE",
    "_apply_code_mode",
]


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

app.include_router(auth.router)
app.include_router(workspace.router)
app.include_router(gateway.router)
app.include_router(admin.router)

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
