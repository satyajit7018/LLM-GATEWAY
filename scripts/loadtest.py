"""Step 6: load test with realistic repeated/near-duplicate/unique traffic.

Fires a concurrent mix of prompts at a running gateway under three configs and
prints a before/after comparison across cost, latency, and hit rate:

    - none      : caching off
    - exact     : exact-match caching only
    - semantic  : exact + semantic caching

Pure httpx + threads, so it needs no Locust install. (A locustfile.py is also
provided for the canonical Step 6 tool.) The server switches config per request
via ?cache_mode=, so one running server covers all three passes.

Start the server first:  uvicorn app.main:app
Then run:                python -m scripts.loadtest
"""
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402

BASE = "http://127.0.0.1:8000"
# Concurrency + traffic scale are env-tunable so the real-API run can stay
# inside a provider's free-tier rate limit. Defaults suit the offline mock.
CONCURRENCY = int(os.getenv("LT_CONCURRENCY", "10"))
SCALE = float(os.getenv("LT_SCALE", "1.0"))  # multiplies the repeat counts

# Realistic traffic: a few popular prompts asked many times (repeats),
# near-duplicate phrasings of them, and some unique one-offs.
POPULAR = [
    "What is the capital of France?",
    "Explain what a load balancer does.",
    "Give me three uses for Redis.",
]
NEAR_DUPES = [
    "capital of France?",
    "what's the capital of France",
    "explain what a load balancer does in one sentence",
    "three uses for Redis please",
]
UNIQUE = [f"Tell me an interesting fact about the number {n}." for n in range(20)]


def build_traffic():
    reps = lambda n: max(1, int(n * SCALE))
    traffic = []
    traffic += POPULAR * reps(15)   # heavy repeats -> exact-cache hits
    traffic += NEAR_DUPES * reps(8)  # paraphrases -> semantic hits
    traffic += UNIQUE[: reps(20)]    # one-offs -> always miss
    return traffic


def _one(prompt, mode):
    r = httpx.post(f"{BASE}/generate", params={"cache_mode": mode},
                   json={"prompt": prompt}, timeout=120)
    r.raise_for_status()
    return r.json()


def run_config(mode, traffic):
    latencies, hits, misses, tokens_billed = [], 0, 0, 0
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        for body in pool.map(lambda p: _one(p, mode), traffic):
            latencies.append(body["latency_ms"])
            if body["cached"]:
                hits += 1
            else:
                misses += 1
                tokens_billed += body["tokens"]  # only misses cost tokens
    wall = time.perf_counter() - t0
    cost = tokens_billed / 1000 * config.COST_PER_1K_TOKENS
    return {
        "mode": mode,
        "requests": len(traffic),
        "hit_rate": round(hits / len(traffic), 3),
        "avg_latency_ms": round(statistics.mean(latencies), 1),
        "p95_latency_ms": round(sorted(latencies)[int(0.95 * len(latencies)) - 1], 1),
        "billed_tokens": tokens_billed,
        "cost_per_1k_req": round(cost / len(traffic) * 1000, 4),
        "wall_s": round(wall, 1),
    }


def main():
    traffic = build_traffic()
    print(f"Traffic: {len(traffic)} requests, concurrency {CONCURRENCY}\n")
    results = []
    for mode in ("none", "exact", "semantic"):
        # Reset caches between passes so each config starts cold and fair.
        httpx.post(f"{BASE}/admin/reset", timeout=120).raise_for_status()
        results.append(run_config(mode, traffic))

    header = ["config", "hit_rate", "avg_latency_ms", "p95_ms",
              "billed_tokens", "cost/1k_req", "wall_s"]
    print("| " + " | ".join(header) + " |")
    print("|" + "|".join(["---"] * len(header)) + "|")
    for r in results:
        print(f"| {r['mode']} | {r['hit_rate']} | {r['avg_latency_ms']} | "
              f"{r['p95_latency_ms']} | {r['billed_tokens']} | "
              f"${r['cost_per_1k_req']} | {r['wall_s']} |")

    base = results[0]
    best = results[-1]
    if base["cost_per_1k_req"]:
        saved = 100 * (1 - best["cost_per_1k_req"] / base["cost_per_1k_req"])
        print(f"\nSemantic vs no-cache: {saved:.0f}% lower cost/req, "
              f"avg latency {base['avg_latency_ms']:.0f}ms -> "
              f"{best['avg_latency_ms']:.0f}ms")


if __name__ == "__main__":
    main()
