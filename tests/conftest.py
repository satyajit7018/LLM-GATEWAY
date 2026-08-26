"""Force a fast, offline configuration for the whole test session.

Set before any `app.*` import so app.config.load_dotenv() (override=False) keeps
these values instead of whatever .env holds.
"""
import os

os.environ["LLM_BACKEND"] = "mock"
os.environ["MOCK_LATENCY_S"] = "0"
os.environ["MOCK_FAIL_RATE"] = "0"
os.environ["REQLOG_PATH"] = ":memory:"
os.environ["SEMANTIC_THRESHOLD"] = "0.65"
os.environ["RATE_LIMIT_RPS"] = "1000"
os.environ["RATE_LIMIT_BURST"] = "2000"
os.environ["PER_USER_RATE_LIMIT_RPS"] = "1000"
os.environ["PER_USER_RATE_LIMIT_BURST"] = "2000"
# Same reasoning as the two limiters above, for the auth-endpoint limiter
# (security review finding) — every TestClient call shares one synthetic
# host ("testclient"), so without this the whole suite's signup/login calls
# across every file would exhaust one real client's tiny default allowance
# almost immediately. Tests that specifically exercise the throttling
# override these back down for themselves (see test_security_review.py).
os.environ["AUTH_RATE_LIMIT_RPS"] = "1000"
os.environ["AUTH_RATE_LIMIT_BURST"] = "2000"
# Gateway tests exercise the LLM pipeline, not the auth gate — turn auth off so
# /generate is reachable without a session. Dedicated auth tests set up their own.
os.environ["AUTH_REQUIRED"] = "0"
# Keep accounts out of the real app_data.db during tests.
os.environ["AUTH_DB_PATH"] = ":memory:"
