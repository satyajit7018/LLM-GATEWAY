"""Generate a before/after table from the persistent request log (Step 7).

Reads app/reqlog's SQLite DB (populated by every /generate call) and prints a
Markdown summary — the honest, cumulative version of the load-test table, drawn
from real traffic rather than a one-off benchmark.

Run:  python -m scripts.report
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import reqlog  # noqa: E402


def main():
    s = reqlog.summary()
    if not s["total"]:
        print("No requests logged yet. Send some traffic through /generate first.")
        return

    order = ["miss", "exact", "semantic"]
    labels = {"miss": "live call (miss)", "exact": "exact hit", "semantic": "semantic hit"}
    print(f"# Request log summary ({s['total']} requests)\n")
    print(f"**Overall hit rate: {s['hit_rate'] * 100:.1f}%**\n")
    print("| result | count | tokens billed | avg latency | cost (USD) |")
    print("|--------|-------|---------------|-------------|------------|")
    total_cost = 0.0
    for k in order:
        r = s["by_result"].get(k)
        if not r:
            continue
        total_cost += r["cost_usd"]
        print(f"| {labels[k]} | {r['count']} | {r['tokens']} | "
              f"{r['avg_latency_ms']:.0f} ms | ${r['cost_usd']:.6f} |")

    miss = s["by_result"].get("miss", {})
    hits = sum(v["count"] for k, v in s["by_result"].items() if k != "miss")
    if miss and hits:
        # What those cache hits would have cost at the miss token-rate.
        miss_rate = miss["cost_usd"] / max(miss["tokens"], 1)
        hit_tokens = sum(v["tokens"] for k, v in s["by_result"].items() if k != "miss")
        would_have = hit_tokens * miss_rate
        print(f"\n**Actual spend:** ${total_cost:.6f} across {s['total']} requests.")
        print(f"**Estimated saved by caching:** ${would_have:.6f} "
              f"({hits} calls served from cache).")


if __name__ == "__main__":
    main()
