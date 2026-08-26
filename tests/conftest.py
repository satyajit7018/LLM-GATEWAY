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
# The lexical embedder buckets tokens into EMBED_DIM slots via a hash — with the
# production default (512) two genuinely-unrelated single-token prompts (e.g.
# tests using a bare uuid4().hex as a "fresh, unique" prompt) can land in the
# same bucket purely by chance and register as a false semantic-cache hit. Rare
# in production with real multi-word prompts, but the test suite mints dozens of
# random hex "fresh prompts" per run, so the birthday-paradox odds of a collision
# somewhere in the run are high enough to flake real assertions (seen on CI:
# tests/test_multiuser_fixes.py::test_log_usage_scoped_per_user_not_global).
# A much larger dimension for tests only makes that collision astronomically
# unlikely without changing the production default.
os.environ["EMBED_DIM"] = "65536"
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
