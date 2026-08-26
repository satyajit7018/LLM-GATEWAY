<div align="center">

# ⚡ LLM Gateway

**A caching gateway for hosted LLMs — with a full multi-user chat app, an in-browser code IDE, and a task board built on top.**

`748 ms → <1 ms cache hits` · `~71% fewer billed tokens` · `6 providers, 10+ models` · `100+ tests, CI green`

</div>

---

## What it is

What started as a **production-style caching gateway** in front of hosted LLM APIs (Groq, Gemini, OpenRouter, Mistral, and more) has grown into a full personal AI workspace built on that gateway: real accounts, a chat UI, an in-browser code editor with live sandboxed preview and local execution, and a task board for running more than one build at a time. The caching core is still the foundation — a prompt is served from cache when it can be, near-instant and free, and only calls a real model when the answer is genuinely new.

## What it does

**Core engine**
- **Two-layer caching** — *exact-match* for identical prompts and *semantic* for reworded ones, so repeated and near-duplicate questions cost ~0 ms and 0 tokens.
- **Multi-provider routing** — Groq, Google Gemini, OpenRouter, and Mistral today; adding a provider is a base URL + key + model name.
- **Resilience** — token-bucket rate limiting (including a dedicated, tighter limit on auth endpoints), retry with exponential backoff, and a circuit breaker so a failing provider can't cascade.
- **Multimodal** — analyze photos / camera shots with vision models; read PDFs and text files straight into the prompt.
- **Streaming + memory** — token-by-token responses with stop / regenerate, and an optional conversation-memory toggle.
- **Observability** — Prometheus metrics, a SQLite request log, and live in-app dashboards for hit rate, latency, and cost.

**Accounts & access**
- **Real multi-user accounts** — signup/login, email verification, forgot/reset password, change password, sign out everywhere, and self-serve account deletion.
- **Bring your own key** — connect a provider's API key to unlock its premium models; the key is validated with a real test call *before* it's ever saved, so a typo is caught immediately, not on first real use.
- **Free daily quota** — a per-user token allowance on the app's own shared models, independent of BYO-key usage.
- **Server-side conversation sync** — chat history follows a signed-in user across browsers/devices, not just one machine's `localStorage`.

**Build mode — a real code workspace**
- **Live sandboxed preview with a build trace** — every code answer auto-runs in a sandboxed iframe and self-checks for console errors; the chat message shows a live pipeline (*writing code → running & checking → ✓ runs clean*) instead of going silent mid-way.
- **Multi-file Monaco editor** — the same editor core as VS Code, vendored offline, with real IntelliSense for JS/TS/CSS/HTML/JSON and Python via Jedi, plus project-wide search.
- **Local execution** — real interpreters (Python, Node, Bash, Ruby…) with interactive stdin, pip/npm installs, and a persistent per-project workspace. Loopback-only by default so it can't become remote code execution.
- **Checkpoints** — every message that changes the code carries a snapshot of the project at that point; "Revert to here" restores it exactly, no matter what's changed since.
- **Publish** — one click gets a stable, shareable link to the rendered page, hosted by the same server — no third-party deploy account needed. (Served with a locked-down CSP so a published page can't ride a visitor's session to call the app's own API.)
- **Screenshot → code** — attach a design reference or screenshot in Build mode and it's read as a layout to reproduce.

**Task board**
- **Kanban view** of every code tab (Not started / In progress / Needs fix / Done), live-derived from each task's actual status, with drag-to-override and a short goal note per task.
- **Real background concurrency** — start a Build, switch to a different task, start another: both actually run at once, including each one's live-preview check.
- **Fork & merge** — clone a task's project into a new, independently-running linked task, then diff the two back together file-by-file and pick a winner per file.

## The request pipeline

```
Request → Rate limit → Exact cache → Semantic cache → Circuit breaker + retry → Provider
                          │ hit           │ hit
                          └── <1 ms ──────┴── served from cache, 0 tokens
```

A prompt short-circuits at the first cache that can answer it. Only a true miss reaches the model — and every call is logged, counted, and exposed at `/metrics`.

## Stack

- **Backend** — Python · FastAPI · httpx (connection-pooled) · NumPy · Redis · SQLite · pypdf · cryptography (Fernet)
- **Frontend** — vanilla HTML/CSS/JS, no framework (SSE streaming, custom markdown renderer, drag-and-drop, light/dark, responsive to mobile)
- **Editor** — Monaco (VS Code's editor core), vendored offline; Jedi for Python IntelliSense
- **Ops & testing** — Docker Compose · Prometheus · Grafana · GitHub Actions · pytest

## Results (measured on real Groq calls)

- **748 ms** live-call latency → **<1 ms** cache hits
- **~71%** fewer billed tokens on realistic repeated + near-duplicate traffic
- **100+ passing tests**, CI on Python 3.9 / 3.11 / 3.12
- **Zero-setup by default** — runs fully offline on a mock backend; real services (a provider, Redis, email) are one env var away

## Engineering highlights

- **Testing caught confidently-wrong answers.** A scenario pass found the semantic cache returning *"Rome"* for *"capital of Spain"* — same sentence shape, one different word. Fixed by weighting content words over function words, and locked with a regression test.
- **A security review before wider use** caught three real gaps once the app started handling real accounts and other people's API keys: an unauthenticated admin endpoint, no throttling on login/signup, and published pages served with no isolation from the app's own session — all fixed and regression-tested, not just reported (see [SECURITY.md](SECURITY.md)).
- **Honest design tradeoffs.** Requests are single-turn by default so caching stays meaningful, with conversation memory as an explicit opt-in — and the cache key includes model + context, so a hit is always a *correct* hit.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000      # → http://localhost:8000
pytest                                 # 100+ tests, mock backend, no key needed
```

Point it at a real provider with one env var (`LLM_BACKEND=groq` + `LLM_API_KEY=…`, or add `GEMINI_API_KEY=…`). Create accounts and BYO keys by setting `APP_ENCRYPTION_KEY` (see [README.md](README.md)). Or run the full stack — gateway + Redis + Prometheus + Grafana — with `docker compose up --build`.
