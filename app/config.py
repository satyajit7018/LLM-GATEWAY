"""Central configuration, all overridable via environment variables.

Defaults are chosen so the whole app runs with ZERO external setup:
no LLM API key and no Redis server required. Set the env vars below to
flip any component over to the real thing.
"""
import os

from dotenv import load_dotenv

load_dotenv()


# --- LLM backend ---------------------------------------------------------
# LLM_BACKEND: "mock" (default, deterministic, no key) or "groq"/"openai".
LLM_BACKEND = os.getenv("LLM_BACKEND", "mock").lower()
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "llama-3.1-8b-instant")
# Vision-capable model used when a request includes images (photo/camera).
# qwen/qwen3.6-27b is multimodal on Groq; override for other providers.
LLM_VISION_MODEL = os.getenv("LLM_VISION_MODEL", "qwen/qwen3.6-27b")

# --- Multi-provider registry --------------------------------------------
# Every provider is OpenAI-compatible, so adding one is just a base URL + key.
# `key_env` names the env var holding its API key; the default backend also
# falls back to LLM_API_KEY so existing single-provider setups keep working.
PROVIDERS = {
    "groq": {"base_url": "https://api.groq.com/openai/v1/chat/completions",
             "key_env": "GROQ_API_KEY"},
    "openai": {"base_url": "https://api.openai.com/v1/chat/completions",
               "key_env": "OPENAI_API_KEY"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
               "key_env": "GEMINI_API_KEY", "stream": False},  # OpenAI-compat SSE is slow/flaky → one-shot
    "openrouter": {"base_url": "https://openrouter.ai/api/v1/chat/completions",
                   "key_env": "OPENROUTER_API_KEY"},
    "cerebras": {"base_url": "https://api.cerebras.ai/v1/chat/completions",
                 "key_env": "CEREBRAS_API_KEY"},
    "mistral": {"base_url": "https://api.mistral.ai/v1/chat/completions",
                "key_env": "MISTRAL_API_KEY"},
}

# Curated model catalog the UI offers. `id` is provider-prefixed and is what the
# client sends. `vision: True` marks image-capable models. Add rows freely.
MODEL_CATALOG = [
    {"id": "groq/gpt-oss-20b", "label": "GPT-OSS 20B — fast",
     "provider": "groq", "model": "openai/gpt-oss-20b", "best_for": "speed, general chat"},
    {"id": "groq/gpt-oss-120b", "label": "GPT-OSS 120B — reasoning",
     "provider": "groq", "model": "openai/gpt-oss-120b", "best_for": "reasoning, math"},
    {"id": "groq/qwen3.6-27b", "label": "Qwen 3.6 — vision",
     "provider": "groq", "model": "qwen/qwen3.6-27b", "vision": True, "best_for": "images, vision"},
    {"id": "groq/compound", "label": "Groq Compound — agentic/web",
     "provider": "groq", "model": "groq/compound", "best_for": "tool use, web search"},
    {"id": "gemini/flash-lite", "label": "Gemini Flash Lite — fast vision, 1M ctx",
     "provider": "gemini", "model": "gemini-flash-lite-latest", "vision": True, "best_for": "fast vision, long documents"},
    {"id": "gemini/flash", "label": "Gemini Flash — vision, deeper reasoning (slower)",
     "provider": "gemini", "model": "gemini-flash-latest", "vision": True, "best_for": "vision, harder questions"},
    {"id": "gemini/pro", "label": "Gemini Pro — deep reasoning (low free quota)",
     "provider": "gemini", "model": "gemini-pro-latest", "vision": True, "best_for": "hardest reasoning, analysis"},
    {"id": "openrouter/deepseek-r1", "label": "DeepSeek R1 — reasoning",
     "provider": "openrouter", "model": "deepseek/deepseek-r1:free", "best_for": "hard reasoning, math"},
    {"id": "openrouter/qwen-coder", "label": "Qwen Coder — coding",
     "provider": "openrouter", "model": "qwen/qwen-2.5-coder-32b-instruct:free", "best_for": "coding"},
    {"id": "mistral/codestral", "label": "Codestral — coding",
     "provider": "mistral", "model": "codestral-latest", "best_for": "coding"},
]
MODEL_BY_ID = {m["id"]: m for m in MODEL_CATALOG}

# Default model = the configured backend + LLM_MODEL, mapped onto a catalog id.
DEFAULT_MODEL_ID = os.getenv("DEFAULT_MODEL_ID", f"{LLM_BACKEND}/{LLM_MODEL}")
if DEFAULT_MODEL_ID not in MODEL_BY_ID:
    DEFAULT_MODEL_ID = "groq/gpt-oss-20b"
# Vision fallback (used when a request has images but the picked model can't see).
VISION_MODEL_ID = os.getenv("VISION_MODEL_ID", "groq/qwen3.6-27b")


def provider_key(provider: str) -> str:
    """Resolve a provider's API key from its env var, falling back to
    LLM_API_KEY when it is the configured default backend."""
    spec = PROVIDERS.get(provider, {})
    key = os.getenv(spec.get("key_env", ""), "")
    if not key and provider == LLM_BACKEND:
        key = LLM_API_KEY
    return key


# Back-compat: the original single-provider base URL.
LLM_BASE_URL = os.getenv(
    "LLM_BASE_URL", PROVIDERS.get(LLM_BACKEND, {}).get("base_url", ""))

# Simulated latency (seconds) for the mock backend, so cache speedups are
# visible in the baseline/demo numbers the way a real API call would be.
MOCK_LATENCY_S = float(os.getenv("MOCK_LATENCY_S", "1.8"))

# --- local code execution ------------------------------------------------
# Runs the code editor's projects with the real interpreters on this machine
# (Python/Node/…). This executes arbitrary code with the server's privileges,
# so it's meant for a locally-run instance. On by default; set to 0 to disable.
ENABLE_LOCAL_RUN = os.getenv("ENABLE_LOCAL_RUN", "1") not in ("0", "false", "False", "")
# Only loopback clients may hit /run and /workspace by default — this blocks
# remote code execution even if the server is bound to 0.0.0.0. Set to 1 to
# allow non-local clients (do this only behind your own trusted auth).
RUN_ALLOW_REMOTE = os.getenv("RUN_ALLOW_REMOTE", "0") in ("1", "true", "True")
# Optional shared secret required in the X-Run-Token header for /run + /workspace.
RUN_TOKEN = os.getenv("RUN_TOKEN", "")

# --- accounts / auth (hybrid multi-user build, Phase 1) ------------------
# When on, chat endpoints require a logged-in user. Default on; the test suite
# turns it off so the gateway logic can be tested without a session.
AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "1") not in ("0", "false", "False", "")
# Set the session cookie's Secure flag (requires HTTPS). Off for local http dev;
# MUST be 1 in any public deployment.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") in ("1", "true", "True")
# Where user accounts + sessions live (SQLite).
AUTH_DB_PATH = os.getenv("AUTH_DB_PATH",
                         os.path.join(os.path.dirname(__file__), "..", "app_data.db"))

# --- bring-your-own provider keys (hybrid build, Phase 2) -----------------
# Fernet key (44-char urlsafe-base64) encrypting users' own API keys at rest.
# Generate one with: python -c "from cryptography.fernet import Fernet as F; print(F.generate_key().decode())"
# Without this set, the BYO-key feature is disabled (never store keys unencrypted).
APP_ENCRYPTION_KEY = os.getenv("APP_ENCRYPTION_KEY", "")

# --- free-tier per-user quotas (hybrid build, Phase 3) --------------------
# Daily token cap on the app's OWN provider keys ("free" models), per user.
# Only counts real upstream calls — cache hits are free and don't count, and a
# user's own bring-your-own key never counts (it doesn't cost us anything).
# Resets at UTC midnight. 0 = unlimited (quota disabled).
FREE_DAILY_TOKEN_LIMIT = int(os.getenv("FREE_DAILY_TOKEN_LIMIT", "50000"))

# --- invite-gated signup (hybrid build, Phase 4) --------------------------
# When on, /auth/signup requires a valid, unused invite code. Off by default
# (open signup) to match the existing local/demo behavior. Generate codes with
# scripts/create_invite.py.
SIGNUP_REQUIRES_INVITE = os.getenv("SIGNUP_REQUIRES_INVITE", "0") in ("1", "true", "True")

# --- password reset via email (hybrid build, Phase 4) ---------------------
# Real delivery: set all of SMTP_HOST/SMTP_USER/SMTP_PASS to send via smtplib
# (stdlib — no extra dependency). Without SMTP configured, reset links are
# logged to the server console instead of emailed — fine for local/demo use,
# NOT a substitute for real email delivery in any public deployment.
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "no-reply@llm-gateway.local")
# Base URL used to build the reset link users click (no trailing slash).
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")

# Inject synthetic failures into the mock backend (0.0-1.0) so the retry /
# circuit-breaker logic (Step 5) can be exercised offline. Default: never fail.
MOCK_FAIL_RATE = float(os.getenv("MOCK_FAIL_RATE", "0.0"))

# Cost model for the load-test cost comparison (Step 6). A cache hit costs 0
# because no API call is made. Default is a blended estimate for Groq
# llama-3.1-8b-instant (~$0.05/1M in, ~$0.08/1M out => ~$0.065/1M = this per 1K).
COST_PER_1K_TOKENS = float(os.getenv("COST_PER_1K_TOKENS", "0.000065"))


# --- Cache ---------------------------------------------------------------
# If Redis is reachable it is used; otherwise the app falls back to an
# in-process dict automatically (see app/cache.py). No config needed.
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CACHE_TTL_S = int(os.getenv("CACHE_TTL_S", str(60 * 60 * 24)))  # 24h
# Max entries the in-memory fallback backend holds before FIFO-evicting the
# oldest. Bounds memory on a long-running server (Redis handles its own
# eviction). Ignored when Redis is in use.
CACHE_MAX_ENTRIES = int(os.getenv("CACHE_MAX_ENTRIES", "5000"))


# --- Semantic cache (Step 4) --------------------------------------------
# EMBED_BACKEND: "lexical" (default, offline, no model download) or "sbert"
# (real sentence-transformers/all-MiniLM-L6-v2; pip install sentence-transformers).
EMBED_BACKEND = os.getenv("EMBED_BACKEND", "lexical").lower()
EMBED_MODEL = os.getenv("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
EMBED_DIM = int(os.getenv("EMBED_DIM", "512"))  # lexical hashed-vector dim
SEMANTIC_CACHE_ENABLED = os.getenv("SEMANTIC_CACHE_ENABLED", "1") == "1"
# 0.65 suits the default lexical embedder: measured near-duplicate prompts
# score 0.70-0.82 while unrelated prompts score ~0.00, so this sits safely in
# the gap. Raise toward ~0.8 when using EMBED_BACKEND=sbert (denser vectors).
SEMANTIC_THRESHOLD = float(os.getenv("SEMANTIC_THRESHOLD", "0.65"))
SEMANTIC_MAX_ENTRIES = int(os.getenv("SEMANTIC_MAX_ENTRIES", "5000"))


# --- Rate limiting + resilience (Step 5) --------------------------------
RATE_LIMIT_RPS = float(os.getenv("RATE_LIMIT_RPS", "50"))   # sustained req/s
RATE_LIMIT_BURST = float(os.getenv("RATE_LIMIT_BURST", "100"))  # bucket size

# Per-account rate limit (hybrid multi-user build) — the limits above are a
# GLOBAL cap shared by every request (protects the upstream provider / this
# server in aggregate); this is a SEPARATE, tighter cap per logged-in user (or
# per client IP when nobody's logged in) so one busy account can't 429 every
# other account by itself. Deliberately tighter than the global default.
PER_USER_RATE_LIMIT_RPS = float(os.getenv("PER_USER_RATE_LIMIT_RPS", "10"))
PER_USER_RATE_LIMIT_BURST = float(os.getenv("PER_USER_RATE_LIMIT_BURST", "20"))

# Security review finding: auth endpoints (login, signup, forgot-password,
# resend-verification) had no throttling at all — unlimited password
# guessing against a known email, and forgot/resend can be used to spam a
# victim's inbox. Deliberately much tighter than the generation limiter
# above and keyed by client IP (there's no user yet at login time): a slow
# trickle after a handful of attempts, not a hard per-minute window, so a
# legitimate user who mistypes their password a few times in a row is never
# blocked, only someone hammering it continuously.
AUTH_RATE_LIMIT_RPS = float(os.getenv("AUTH_RATE_LIMIT_RPS", "0.1"))   # 1 every 10s, sustained
AUTH_RATE_LIMIT_BURST = float(os.getenv("AUTH_RATE_LIMIT_BURST", "5"))

LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))
LLM_BACKOFF_BASE_S = float(os.getenv("LLM_BACKOFF_BASE_S", "0.2"))

CIRCUIT_FAIL_THRESHOLD = int(os.getenv("CIRCUIT_FAIL_THRESHOLD", "5"))
CIRCUIT_RESET_S = float(os.getenv("CIRCUIT_RESET_S", "10"))
