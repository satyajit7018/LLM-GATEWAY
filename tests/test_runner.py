"""Tests for the local code runner and its HTTP guardrails."""
import sys
import time

import pytest
from fastapi.testclient import TestClient

from app import config, runner
from app.main import app

client = TestClient(app)


def _drain(sid, timeout=15):
    """Collect (channel, text) events from a session until it finishes."""
    out, deadline = [], time.time() + timeout
    for ev in runner.stream_run(sid):
        out.append(ev)
        if ev[0] == "exit" or time.time() > deadline:
            break
    return out


def _text(events):
    return "".join(t for c, t in events if c in ("out", "sys"))


# ---- pure helpers ----
def test_available_languages_includes_python():
    langs = {l["name"]: l for l in runner.available_languages()}
    assert langs["Python"]["available"] is True


def test_safe_name_blocks_traversal():
    assert runner._safe_name("../../etc/passwd") == "etc/passwd"
    assert runner._safe_name("/abs/path.py") == "abs/path.py"


def test_safe_join_rejects_escape():
    base = runner.workspace_dir("unit-safe")
    assert runner._safe_join(base, "../secret") is None
    assert runner._safe_join(base, "ok.txt") is not None


# ---- execution ----
def test_runs_python_and_reports_exit_code():
    sid = runner.start_run(
        [{"name": "m.py", "content": "print('hi', 2 + 2)"}], "m.py", timeout=15)
    events = _drain(sid)
    assert ("out", "hi 4\n") in events or "hi 4" in _text(events)
    assert events[-1] == ("exit", "0")


def test_nonzero_exit_is_reported():
    sid = runner.start_run(
        [{"name": "m.py", "content": "import sys; sys.exit(3)"}], "m.py", timeout=15)
    events = _drain(sid)
    assert events[-1] == ("exit", "3")


def test_unknown_extension_is_handled():
    sid = runner.start_run([{"name": "m.xyz", "content": "x"}], "m.xyz", timeout=5)
    events = _drain(sid)
    assert "No local runner" in _text(events)


def test_interactive_stdin():
    sid = runner.start_run(
        [{"name": "m.py", "content": "print('you said', input())"}], "m.py", timeout=15)
    # give the process a moment to start and reach input()
    for _ in range(50):
        if runner._SESSIONS[sid].proc:
            break
        time.sleep(0.05)
    assert runner.send_input(sid, "ping") is True
    events = _drain(sid)
    assert "you said ping" in _text(events)


def test_persistent_workspace_round_trip():
    ws = "unit-persist"
    sid1 = runner.start_run(
        [{"name": "w.py", "content": "open('kept.txt','w').write('yes')"}], "w.py",
        workspace=ws, timeout=15)
    _drain(sid1)
    sid2 = runner.start_run(
        [{"name": "r.py", "content": "print(open('kept.txt').read())"}], "r.py",
        workspace=ws, timeout=15)
    events = _drain(sid2)
    assert "yes" in _text(events)
    assert any(f["path"] == "kept.txt" for f in runner.list_workspace(ws))


# ---- HTTP guardrails ----
def test_run_rejects_non_local_client_by_default():
    # TestClient presents a non-loopback host ("testclient"), so /run is blocked.
    r = client.post("/run/start", json={"entry": "m.py", "files": []})
    assert r.status_code == 403


def test_run_env_lists_runtimes():
    body = client.get("/run/env").json()
    assert body["enabled"] is True
    assert any(l["name"] == "Python" for l in body["languages"])


def test_run_endpoint_allowed_when_remote_enabled(monkeypatch):
    monkeypatch.setattr(config, "RUN_ALLOW_REMOTE", True)
    r = client.post("/run/start", json={
        "entry": "m.py", "files": [{"name": "m.py", "content": "print('ok')"}]})
    assert r.status_code == 200
    sid = r.json()["session"]
    assert client.get(f"/run/stream/{sid}").status_code == 200


def test_workspace_endpoints_guarded(monkeypatch):
    assert client.get("/workspace/list?ws=x").status_code == 403
    monkeypatch.setattr(config, "RUN_ALLOW_REMOTE", True)
    assert client.get("/workspace/list?ws=x").status_code == 200


# ---- git snapshots ----
@pytest.mark.skipif(not __import__("shutil").which("git"), reason="git not installed")
def test_git_commit_and_history():
    import shutil
    ws = "unit-git"
    shutil.rmtree(runner.workspace_dir(ws), ignore_errors=True)   # isolate from prior runs
    sid = runner.start_run(
        [{"name": "a.py", "content": "print(1)"}], "a.py", workspace=ws, timeout=15)
    _drain(sid)
    first = runner.git_commit(ws, "first")
    assert first["ok"] and first["hash"]
    # nothing changed → refuses to make an empty commit
    assert runner.git_commit(ws, "again")["ok"] is False
    hist = runner.git_history(ws)
    assert hist and hist[0]["msg"] == "first"
    shutil.rmtree(runner.workspace_dir(ws), ignore_errors=True)


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="git not installed")
def test_git_restore():
    import shutil
    ws = "unit-git-restore"
    shutil.rmtree(runner.workspace_dir(ws), ignore_errors=True)
    _drain(runner.start_run([{"name": "a.py", "content": "print('v1')"}], "a.py", workspace=ws, timeout=15))
    v1 = runner.git_commit(ws, "v1")["hash"]
    _drain(runner.start_run([{"name": "a.py", "content": "print('v2')"}], "a.py", workspace=ws, timeout=15))
    runner.git_commit(ws, "v2")
    res = runner.git_restore(ws, v1)
    assert res["ok"]
    a = next(f for f in res["files"] if f["name"] == "a.py")
    assert "v1" in a["content"]
    shutil.rmtree(runner.workspace_dir(ws), ignore_errors=True)
