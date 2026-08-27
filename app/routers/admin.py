"""Operational endpoints: liveness, live counters, Prometheus exposition, and
the loopback-only cache/counter reset used for local load-testing."""
from fastapi import APIRouter, HTTPException, Request, Response

from .. import config, metrics
from ..resilience import breaker
from ..state import _LOOPBACK, _cache, _semantic, _stats, _stats_lock

router = APIRouter()


@router.get("/healthz")
def healthz():
    return {"status": "ok"}


@router.get("/stats")
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


@router.post("/admin/reset")
def admin_reset(request: Request):
    """Clear both caches and counters so a load-test pass starts cold + fair.

    Security review finding: this was public and unauthenticated — anyone
    could wipe the shared cache/metrics with one POST. Restricted to
    loopback the same way local code execution is (see _guard_run in
    routers/workspace.py); this is a local dev/load-testing convenience, not
    something that should be reachable from the open internet.
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


@router.get("/metrics")
def prometheus_metrics():
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)
