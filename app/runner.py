"""Local code execution — runs a project's files with the real interpreter
installed on this machine, with interactive stdin, dependency installs, and a
persistent per-workspace directory.

This is a *real* runner (not a sandbox): it executes arbitrary code with the
same privileges as the server process, like running the script yourself. It is
meant for a locally-run dev tool bound to localhost. Guardrails: an isolated
working directory, a wall-clock timeout that kills the process, and
path-sanitized filenames so a name can't escape the workdir.
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

# Hard cap on how much output a single run may buffer (bytes). A runaway
# `while True: print()` would otherwise exhaust memory; we truncate and kill.
_OUT_CAP = 2_000_000

# Entry file extension -> argv builder (given the entry filename).
_RUNNERS = {
    ".py":  lambda f: [sys.executable, "-u", f],
    ".js":  lambda f: ["node", f],
    ".mjs": lambda f: ["node", f],
    ".cjs": lambda f: ["node", f],
    ".ts":  lambda f: ["npx", "-y", "tsx", f],
    ".sh":  lambda f: ["bash", f],
    ".rb":  lambda f: ["ruby", f],
    ".php": lambda f: ["php", f],
    ".pl":  lambda f: ["perl", f],
    ".lua": lambda f: ["lua", f],
    ".go":  lambda f: ["go", "run", f],
    ".rs":  lambda f: ["rust-script", f],
}

_LANGS = [
    ("Python", sys.executable, ["--version"]),
    ("Node.js", "node", ["--version"]),
    ("TypeScript (tsx)", "tsx", ["--version"]),
    ("Bash", "bash", ["--version"]),
    ("Ruby", "ruby", ["--version"]),
    ("PHP", "php", ["--version"]),
    ("Go", "go", ["version"]),
    ("Perl", "perl", ["--version"]),
]

# Persistent per-workspace directories live here (survive across runs).
WS_ROOT = os.path.join(os.path.expanduser("~"), ".llm-gateway", "workspaces")


def available_languages() -> list[dict]:
    out = []
    for name, cmd, args in _LANGS:
        path = shutil.which(cmd) or (cmd if os.path.exists(cmd) else None)
        ver = ""
        if path:
            try:
                r = subprocess.run([path, *args], capture_output=True, text=True, timeout=5)
                ver = (r.stdout or r.stderr or "").strip().splitlines()[0][:60]
            except Exception:
                ver = ""
        out.append({"name": name, "available": bool(path), "version": ver})
    return out


def _safe_name(name: str) -> str:
    name = (name or "file").replace("\\", "/")
    parts = [p for p in name.split("/") if p not in ("", ".", "..")]
    return os.path.join(*parts) if parts else "file"


def _safe_key(key: str) -> str:
    return "".join(c for c in (key or "") if c.isalnum() or c in "-_")[:64] or "default"


# ------------------------- interactive sessions -------------------------
class _Session:
    def __init__(self):
        self.id = uuid.uuid4().hex[:16]
        self.buf = []            # list of (channel, text)
        self.lock = threading.Lock()
        self.proc = None
        self.done = False
        self.code = None
        self.workdir = None
        self.temp = True
        self.timed_out = False
        self.capped = False
        self.bytes = 0
        self.created = time.time()

    def emit(self, ch, text):
        with self.lock:
            if self.capped and ch == "out":
                return
            self.buf.append((ch, text))
            if ch == "out":
                self.bytes += len(text)
                if self.bytes > _OUT_CAP and not self.capped:
                    self.capped = True
                    self.buf.append(("sys", f"output truncated at {_OUT_CAP // 1000}KB — killing process"))
                    self.kill()

    def kill(self):
        """Kill the whole process group so spawned children don't orphan."""
        p = self.proc
        if not p:
            return
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


_SESSIONS: dict[str, _Session] = {}


def _reap():
    now = time.time()
    for sid in [s for s, x in _SESSIONS.items() if x.done and now - x.created > 600]:
        _SESSIONS.pop(sid, None)


def _stream_proc(sess, argv, cwd, env):
    """Run argv to completion, piping combined output into the session buffer."""
    sess.emit("sys", "$ " + " ".join(argv))
    try:
        p = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, env=env, start_new_session=True)
    except FileNotFoundError:
        sess.emit("sys", f"'{argv[0]}' is not installed or not on PATH."); return 127
    while True:
        data = p.stdout.read1(4096)
        if not data:
            break
        sess.emit("out", data.decode("utf-8", "replace"))
    p.wait()
    return p.returncode


def _worker(sess, files, entry, install, packages, timeout, command=None):
    try:
        for f in files:
            rel = _safe_name(f.get("name", ""))
            dest = os.path.join(sess.workdir, rel)
            os.makedirs(os.path.dirname(dest) or sess.workdir, exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(f.get("content", ""))

        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        libs = os.path.join(sess.workdir, ".pylibs")
        if os.path.isdir(libs):
            env["PYTHONPATH"] = libs + os.pathsep + env.get("PYTHONPATH", "")

        # --- raw shell command (the interactive terminal) ---
        if command is not None:
            shell = shutil.which("bash") or shutil.which("sh") or "sh"
            sess.emit("sys", "$ " + command)
            p = subprocess.Popen([shell, "-lc", command], cwd=sess.workdir, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, start_new_session=True)
            sess.proc = p
            timer = threading.Timer(timeout, lambda: (setattr(sess, "timed_out", True), sess.kill()))
            timer.start()
            try:
                while True:
                    data = p.stdout.read1(4096)
                    if not data:
                        break
                    sess.emit("out", data.decode("utf-8", "replace"))
                p.wait()
            finally:
                timer.cancel()
            sess.code = 124 if sess.timed_out else p.returncode
            if sess.timed_out:
                sess.emit("sys", f"killed after {timeout}s timeout")
            return

        ext = os.path.splitext(entry)[1].lower()
        has_req = os.path.exists(os.path.join(sess.workdir, "requirements.txt"))
        has_pkg = os.path.exists(os.path.join(sess.workdir, "package.json"))
        pkgs = [p for p in (packages or []) if p]

        # --- dependency install (per-workspace, no global pollution) ---
        if install or pkgs:
            if ext == ".py" or has_req or (pkgs and ext not in (".js", ".mjs", ".ts")):
                libs = os.path.join(sess.workdir, ".pylibs")
                env["PYTHONPATH"] = libs + os.pathsep + env.get("PYTHONPATH", "")
                args = [sys.executable, "-m", "pip", "install", "--target", libs, "--disable-pip-version-check"]
                if has_req:
                    args += ["-r", "requirements.txt"]
                args += pkgs
                if has_req or pkgs:
                    sess.emit("sys", "installing Python packages…")
                    _stream_proc(sess, args, sess.workdir, env)
            elif ext in (".js", ".mjs", ".cjs", ".ts") or has_pkg:
                libs = os.path.join(sess.workdir, ".pylibs")
                if os.path.isdir(libs):
                    env.setdefault("PYTHONPATH", libs)
                args = ["npm", "install", "--no-audit", "--no-fund"] + pkgs
                sess.emit("sys", "installing npm packages…")
                _stream_proc(sess, args, sess.workdir, env)
            else:
                libs = os.path.join(sess.workdir, ".pylibs")
                if os.path.isdir(libs):
                    env["PYTHONPATH"] = libs + os.pathsep + env.get("PYTHONPATH", "")
        else:
            libs = os.path.join(sess.workdir, ".pylibs")
            if os.path.isdir(libs):
                env["PYTHONPATH"] = libs + os.pathsep + env.get("PYTHONPATH", "")

        # --- run the entry program ---
        builder = _RUNNERS.get(ext)
        if not builder:
            sess.emit("sys", f"No local runner for '{ext or entry}'."); sess.code = 1; return
        argv = builder(_safe_name(entry))
        if not (shutil.which(argv[0]) or os.path.exists(argv[0])):
            sess.emit("sys", f"'{argv[0]}' is not installed or not on PATH."); sess.code = 127; return

        sess.emit("sys", "$ " + " ".join(argv))
        p = subprocess.Popen(argv, cwd=sess.workdir, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                            start_new_session=True)
        sess.proc = p
        timer = threading.Timer(timeout, lambda: (setattr(sess, "timed_out", True), sess.kill()))
        timer.start()
        try:
            while True:
                data = p.stdout.read1(4096)
                if not data:
                    break
                sess.emit("out", data.decode("utf-8", "replace"))
            p.wait()
        finally:
            timer.cancel()
        sess.code = 124 if sess.timed_out else p.returncode
        sess.emit("sys", f"killed after {timeout}s timeout" if sess.timed_out
                  else f"process exited with code {p.returncode}")
    except Exception as exc:  # pragma: no cover
        sess.emit("sys", f"runner error: {exc}"); sess.code = 1
    finally:
        sess.proc = None; sess.done = True
        if sess.temp:
            shutil.rmtree(sess.workdir, ignore_errors=True)


def start_run(files, entry, install=False, packages=None, timeout=300, workspace=None, command=None) -> str:
    """Start a run session and return its id. Output is read via stream_run.
    If `command` is given, it runs that shell command in the workspace instead
    of executing `entry` (the terminal's arbitrary-command mode)."""
    _reap()
    sess = _Session()
    if workspace:
        sess.workdir = os.path.join(WS_ROOT, _safe_key(workspace)); sess.temp = False
        os.makedirs(sess.workdir, exist_ok=True)
    else:
        sess.workdir = tempfile.mkdtemp(prefix="llmgw-run-"); sess.temp = True
    _SESSIONS[sess.id] = sess
    t = threading.Thread(target=_worker, args=(sess, files, entry, install, packages, timeout, command), daemon=True)
    t.start()
    return sess.id


def stream_run(sid):
    """Yield (channel, text) events for a session until it finishes."""
    sess = _SESSIONS.get(sid)
    if not sess:
        yield ("sys", "no such run session"); yield ("exit", "1"); return
    i = 0
    while True:
        with sess.lock:
            new = sess.buf[i:]; i = len(sess.buf); done = sess.done
        for ch, t in new:
            yield (ch, t)
        if done:
            break
        time.sleep(0.05)
    yield ("exit", str(sess.code if sess.code is not None else 1))


def send_input(sid, text) -> bool:
    sess = _SESSIONS.get(sid)
    if sess and sess.proc and sess.proc.stdin:
        try:
            sess.proc.stdin.write((text + "\n").encode("utf-8")); sess.proc.stdin.flush()
            sess.emit("in", text + "\n"); return True
        except Exception:
            return False
    return False


def kill_run(sid) -> bool:
    sess = _SESSIONS.get(sid)
    if sess and sess.proc:
        sess.kill(); return True
    return False


def shutdown():
    """Kill every live run session — call on server shutdown."""
    for sess in list(_SESSIONS.values()):
        if not sess.done:
            sess.kill()


# ------------------------- workspace files -------------------------
_HIDE_DIRS = {".pylibs", "node_modules", "__pycache__", ".git", ".venv"}


def workspace_dir(ws: str) -> str:
    return os.path.join(WS_ROOT, _safe_key(ws))


def _safe_join(base: str, rel: str):
    target = os.path.normpath(os.path.join(base, rel))
    base = os.path.normpath(base)
    if target != base and not target.startswith(base + os.sep):
        return None
    return target


def list_workspace(ws: str) -> list[dict]:
    """List files a run created/kept in a workspace (skips dependency dirs)."""
    base = workspace_dir(ws)
    if not os.path.isdir(base):
        return []
    out = []
    for root, dirs, fnames in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _HIDE_DIRS]
        for fn in fnames:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, base).replace(os.sep, "/")
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            out.append({"path": rel, "size": size})
    return sorted(out, key=lambda x: x["path"])[:1000]


def workspace_file(ws: str, path: str):
    """Absolute path of a file inside the workspace, or None if unsafe/missing."""
    full = _safe_join(workspace_dir(ws), path)
    return full if full and os.path.isfile(full) else None


# ------------------------- git snapshots -------------------------
def _git(base, *args):
    return subprocess.run(["git", *args], cwd=base, capture_output=True, text=True)


def git_commit(ws: str, message: str) -> dict:
    """Snapshot the workspace: init a repo if needed, then add + commit."""
    base = workspace_dir(ws)
    if not os.path.isdir(base):
        return {"ok": False, "error": "no workspace yet — run some code first"}
    if not shutil.which("git"):
        return {"ok": False, "error": "git is not installed"}
    if not os.path.isdir(os.path.join(base, ".git")):
        _git(base, "init", "-q")
        _git(base, "config", "user.email", "dev@llm-gateway.local")
        _git(base, "config", "user.name", "LLM Gateway")
        gi = os.path.join(base, ".gitignore")
        if not os.path.exists(gi):
            with open(gi, "w") as fh:
                fh.write(".pylibs/\nnode_modules/\n__pycache__/\n")
    _git(base, "add", "-A")
    r = _git(base, "commit", "-q", "-m", (message or "snapshot").strip()[:200])
    blob = (r.stdout + r.stderr).lower()
    if r.returncode != 0:
        if "nothing to commit" in blob:
            return {"ok": False, "error": "nothing changed since the last commit"}
        return {"ok": False, "error": ((r.stderr or r.stdout).strip() or "commit failed")[:200]}
    h = _git(base, "rev-parse", "--short", "HEAD").stdout.strip()
    return {"ok": True, "hash": h}


def git_history(ws: str) -> list[dict]:
    base = workspace_dir(ws)
    if not shutil.which("git") or not os.path.isdir(os.path.join(base, ".git")):
        return []
    r = _git(base, "log", "--pretty=%h\x01%ar\x01%s", "-n", "50")
    out = []
    for line in r.stdout.splitlines():
        parts = line.split("\x01")
        if len(parts) == 3:
            out.append({"hash": parts[0], "when": parts[1], "msg": parts[2]})
    return out


def git_restore(ws: str, ref: str) -> dict:
    """Restore the workspace's files to a past commit (as uncommitted changes),
    and return the restored files so the editor can update."""
    base = workspace_dir(ws)
    if not shutil.which("git") or not os.path.isdir(os.path.join(base, ".git")):
        return {"ok": False, "error": "no git history"}
    ref = "".join(c for c in (ref or "") if c in "0123456789abcdefABCDEF")[:40]
    if not ref:
        return {"ok": False, "error": "bad ref"}
    r = _git(base, "checkout", ref, "--", ".")
    if r.returncode != 0:
        return {"ok": False, "error": ((r.stderr or r.stdout).strip() or "restore failed")[:200]}
    files = []
    for name in _git(base, "ls-tree", "-r", "--name-only", ref).stdout.splitlines():
        if name == ".gitignore":
            continue
        full = _safe_join(base, name)
        if full and os.path.isfile(full):
            try:
                with open(full, encoding="utf-8") as fh:
                    files.append({"name": name, "content": fh.read()})
            except Exception:
                pass
    return {"ok": True, "files": files}
