"""Step 1: uncached baseline.

Sends N prompts straight to the LLM backend (no gateway, no cache) and records
latency + estimated tokens per call to a CSV. This is the honest "before"
number every later comparison is measured against.

Run:  python -m scripts.baseline
"""
import csv
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402
from app.llm import generate  # noqa: E402

PROMPTS = [
    "What is the capital of France?",
    "Explain what a load balancer does in one sentence.",
    "Give me three uses for Redis.",
    "What is exponential backoff?",
    "Summarize what an LLM gateway is.",
]

OUT = Path(__file__).resolve().parents[1] / "baseline_results.csv"


def main():
    print(f"Backend={config.LLM_BACKEND} model={config.LLM_MODEL}")
    rows = []
    for i, prompt in enumerate(PROMPTS, 1):
        start = time.perf_counter()
        result = generate(prompt)
        elapsed_ms = (time.perf_counter() - start) * 1000
        rows.append((i, prompt, round(elapsed_ms, 2), result["tokens"]))
        print(f"  {i}. {elapsed_ms:8.1f} ms  {result['tokens']:>4} tok  {prompt}")

    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["n", "prompt", "latency_ms", "tokens"])
        w.writerows(rows)

    latencies = [r[2] for r in rows]
    print("\nBaseline summary (uncached):")
    print(f"  calls        : {len(rows)}")
    print(f"  avg latency  : {statistics.mean(latencies):.1f} ms")
    print(f"  total tokens : {sum(r[3] for r in rows)}")
    print(f"  written to   : {OUT}")


if __name__ == "__main__":
    main()
