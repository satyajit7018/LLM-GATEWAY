"""Prometheus metrics (Step 7).

Exposes counters/histograms scraped at GET /metrics. If prometheus_client is
not installed, this degrades to no-ops so the app still runs.
"""
try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Histogram,
        generate_latest,
    )

    _ENABLED = True
except Exception:  # pragma: no cover - optional dep
    _ENABLED = False
    CONTENT_TYPE_LATEST = "text/plain"

if _ENABLED:
    REQUESTS = Counter(
        "gateway_requests_total", "Requests by result", ["result"]
    )
    LATENCY = Histogram(
        "gateway_request_latency_seconds", "End-to-end request latency"
    )
    ERRORS = Counter("gateway_errors_total", "Upstream/gateway errors", ["kind"])


def record_request(result: str, latency_s: float):
    if _ENABLED:
        REQUESTS.labels(result=result).inc()
        LATENCY.observe(latency_s)


def record_error(kind: str):
    if _ENABLED:
        ERRORS.labels(kind=kind).inc()


def render():
    if _ENABLED:
        return generate_latest(), CONTENT_TYPE_LATEST
    return b"# prometheus_client not installed\n", CONTENT_TYPE_LATEST


def enabled() -> bool:
    return _ENABLED
