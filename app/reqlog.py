"""Structured request log (SQLite).

Every /generate call is appended as one row so real before/after numbers can be
reconstructed later (see scripts/report.py). SQLite keeps it dependency-free and
durable across restarts; set REQLOG_PATH=:memory: to disable persistence.
"""
import os
import sqlite3
import threading
import time

DB_PATH = os.getenv("REQLOG_PATH",
                    os.path.join(os.path.dirname(__file__), "..", "requests.db"))

_lock = threading.Lock()
_conn = None


def _connect():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        # WAL lets readers (e.g. /log/usage) proceed while a write is in
        # flight instead of blocking on the single rollback-journal lock —
        # cheap to turn on, real benefit since requests are logged from every
        # /generate call. No-ops harmlessly on ":memory:" (used in tests).
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS requests (
                   ts REAL, result TEXT, cache_type TEXT, model TEXT,
                   tokens INTEGER, latency_ms REAL, cost_usd REAL,
                   has_images INTEGER, has_files INTEGER, streamed INTEGER
               )"""
        )
        # Migration: a pre-existing requests.db predates the `user_id` column
        # (added for the hybrid multi-user build) — CREATE TABLE IF NOT EXISTS
        # is a no-op on an existing table, so add the column explicitly rather
        # than silently keep logging usage with no user attribution.
        cols = {row[1] for row in _conn.execute("PRAGMA table_info(requests)")}
        if "user_id" not in cols:
            _conn.execute("ALTER TABLE requests ADD COLUMN user_id INTEGER")
        # This table has no primary key and grows by one row per /generate
        # call forever — without this, usage_by_model(user_id=...) (called on
        # every page load) was a full table scan that only gets slower as
        # history accumulates.
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_user ON requests(user_id)")
        _conn.commit()
    return _conn


def log(result, cache_type, model, tokens, latency_ms, cost_usd,
        has_images=False, has_files=False, streamed=False, user_id=None):
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO requests (ts, result, cache_type, model, tokens, latency_ms, "
            "cost_usd, has_images, has_files, streamed, user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), result, cache_type, model, int(tokens or 0),
             round(latency_ms, 2), round(cost_usd, 8),
             int(has_images), int(has_files), int(streamed), user_id),
        )
        conn.commit()


def usage_by_model(user_id=None) -> dict:
    """Per-model token/request/cost totals — real usage only (misses), so a
    cache hit (0 tokens billed) doesn't inflate a model's "spent" count.
    Scoped to one user's own usage when `user_id` is given (the normal case —
    this is what a logged-in user sees as "their" usage/cost); omitting it
    returns the whole server's aggregate, for operational/ops use only."""
    with _lock:
        conn = _connect()
        if user_id is None:
            rows = conn.execute(
                "SELECT model, COUNT(*), COALESCE(SUM(tokens),0), COALESCE(SUM(cost_usd),0) "
                "FROM requests WHERE result = 'miss' AND model != '' GROUP BY model"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT model, COUNT(*), COALESCE(SUM(tokens),0), COALESCE(SUM(cost_usd),0) "
                "FROM requests WHERE result = 'miss' AND model != '' AND user_id = ? GROUP BY model",
                (user_id,)
            ).fetchall()
    return {r[0]: {"requests": r[1], "tokens": r[2], "cost_usd": round(r[3], 6)} for r in rows}


def summary() -> dict:
    """Aggregate stats for the /log/summary endpoint and report script."""
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT result, COUNT(*), COALESCE(SUM(tokens),0), "
            "COALESCE(AVG(latency_ms),0), COALESCE(SUM(cost_usd),0) "
            "FROM requests GROUP BY result"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    by_result = {r[0]: {"count": r[1], "tokens": r[2],
                        "avg_latency_ms": round(r[3], 2), "cost_usd": round(r[4], 8)}
                 for r in rows}
    hits = sum(v["count"] for k, v in by_result.items() if k != "miss")
    return {
        "total": total,
        "hit_rate": round(hits / total, 4) if total else 0.0,
        "by_result": by_result,
    }
