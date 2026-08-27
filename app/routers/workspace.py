"""Local code execution (real interpreters on this machine), the persistent
per-project workspace filesystem + git snapshots, and Python IntelliSense.

Everything here is loopback-only by default (see _guard_run / _guard_lsp) —
this is a self-hosted, single-user feature, not something meant to be
reachable from beyond the machine running the server.
"""
import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import config, runner
from ..state import _LOOPBACK

router = APIRouter()


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


@router.get("/run/env")
def run_env():
    """Which language runtimes are installed on this machine."""
    return {"enabled": config.ENABLE_LOCAL_RUN, "languages": runner.available_languages()}


@router.post("/run/start")
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


@router.get("/run/stream/{sid}")
def run_stream(sid: str, request: Request):
    """Stream a run session's output as SSE until the process exits."""
    _guard_run(request)
    def gen():
        for channel, text in runner.stream_run(sid):
            yield f"data: {json.dumps({'channel': channel, 'text': text})}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/run/input")
def run_input(req: RunInput, request: Request):
    """Send a line to a running process's stdin (interactive input)."""
    _guard_run(request)
    return {"ok": runner.send_input(req.session, req.text)}


@router.post("/run/kill")
def run_kill(req: RunInput, request: Request):
    _guard_run(request)
    return {"ok": runner.kill_run(req.session)}


@router.get("/workspace/list")
def workspace_list(request: Request, ws: str):
    """Files a run created or kept in a persistent workspace."""
    _guard_run(request)
    return {"workspace": ws, "files": runner.list_workspace(ws)}


@router.get("/workspace/file")
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


@router.post("/workspace/commit")
def workspace_commit(req: GitCommit, request: Request):
    """Git-snapshot a workspace's files."""
    _guard_run(request)
    return runner.git_commit(req.ws, req.message)


@router.get("/workspace/history")
def workspace_history(request: Request, ws: str):
    _guard_run(request)
    return {"history": runner.git_history(ws)}


class GitRestore(BaseModel):
    ws: str = Field(max_length=64)
    ref: str = Field(max_length=64)


@router.post("/workspace/restore")
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


@router.post("/lsp/python")
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
