"""End-to-end demo: fires traffic at a running gateway and prints the
cache hit rate + the latency difference between a miss and a hit.

Start the server first:  uvicorn app.main:app
Then run:                python -m scripts.demo
"""
import statistics
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8000"

# Deliberately repeats prompts so exact-match caching has something to hit.
UNIQUE = [
    "What is the capital of France?",
    "Explain what a load balancer does in one sentence.",
    "Give me three uses for Redis.",
]
TRAFFIC = UNIQUE * 4  # each prompt: 1 miss + 3 hits


def main():
    miss_lat, hit_lat = [], []
    for prompt in TRAFFIC:
        r = httpx.post(f"{BASE}/generate", json={"prompt": prompt}, timeout=120)
        r.raise_for_status()
        body = r.json()
        (hit_lat if body["cached"] else miss_lat).append(body["latency_ms"])

    stats = httpx.get(f"{BASE}/stats").json()
    print("Stats:", stats)
    if miss_lat:
        print(f"avg MISS latency: {statistics.mean(miss_lat):8.1f} ms")
    if hit_lat:
        print(f"avg HIT  latency: {statistics.mean(hit_lat):8.1f} ms")
    if miss_lat and hit_lat:
        speedup = statistics.mean(miss_lat) / statistics.mean(hit_lat)
        print(f"cache speedup   : {speedup:.0f}x faster on hits")


if __name__ == "__main__":
    main()
