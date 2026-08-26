"""End-to-end API tests against the mock backend (FastAPI TestClient)."""
import json

import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset():
    # /admin/reset is now loopback-only (security review finding — it used
    # to be public and unauthenticated); TestClient presents a non-loopback
    # host ("testclient") by design, same as the /run guard's own tests
    # rely on to prove that block works. Reset the same module-level state
    # directly instead of going through the now-guarded HTTP endpoint.
    main._cache.clear()
    main._semantic.clear()
    with main._stats_lock:
        for k in main._stats:
            main._stats[k] = 0
    yield


def test_healthz():
    assert client.get("/healthz").json()["status"] == "ok"


def test_generate_miss_then_exact_hit():
    body = {"prompt": "unique prompt for hit test"}
    first = client.post("/generate", json=body).json()
    assert first["cached"] is False and first["cache_type"] == "none"
    second = client.post("/generate", json=body).json()
    assert second["cached"] is True and second["cache_type"] == "exact"
    assert second["text"] == first["text"]


def test_semantic_hit_on_near_duplicate():
    client.post("/generate", json={"prompt": "What is the capital of France?"})
    r = client.post("/generate", json={"prompt": "capital of France?"}).json()
    assert r["cached"] is True and r["cache_type"] == "semantic"
    assert r["match_score"] >= 0.65


def test_empty_payload_rejected():
    assert client.post("/generate", json={"prompt": ""}).status_code == 422


def test_stats_and_hitrate():
    client.post("/generate", json={"prompt": "abc"})
    client.post("/generate", json={"prompt": "abc"})  # exact hit
    s = client.get("/stats").json()
    assert s["hits_exact"] == 1 and s["misses"] == 1
    assert s["hit_rate"] == 0.5


def test_quota_mock_not_available():
    q = client.get("/quota").json()
    assert q["backend"] == "mock" and q["available"] is False


def test_image_bypasses_semantic_cache():
    img = "data:image/png;base64,AAAA"
    r = client.post("/generate", json={"prompt": "describe", "images": [img]}).json()
    assert r["cache_type"] == "none"  # first time, a miss
    r2 = client.post("/generate", json={"prompt": "describe", "images": [img]}).json()
    assert r2["cached"] is True and r2["cache_type"] == "exact"


def test_stream_emits_deltas_and_done():
    deltas, done_frame = [], None
    with client.stream("POST", "/generate/stream",
                       json={"prompt": "stream me a sentence"}) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            obj = json.loads(data)
            if "delta" in obj:
                deltas.append(obj["delta"])
            elif obj.get("done"):
                done_frame = obj
    assert deltas, "expected streamed deltas"
    assert done_frame and done_frame["cached"] is False


def test_log_summary_counts_requests():
    client.post("/generate", json={"prompt": "log me"})
    s = client.get("/log/summary").json()
    assert s["total"] >= 1
