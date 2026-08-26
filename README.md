# LLM Gateway

[![CI](https://github.com/satyajit7018/LLM-GATEWAY/actions/workflows/ci.yml/badge.svg)](https://github.com/satyajit7018/LLM-GATEWAY/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.9%20%7C%203.11%20%7C%203.12-blue)
![Zero setup](https://img.shields.io/badge/setup-zero%20external%20services-brightgreen)

A caching gateway for hosted LLM APIs that makes them cheaper and faster at
scale — exact-match + semantic caching, rate limiting, retry/backoff, circuit
breaking, and Prometheus metrics — built out into a full personal AI
workspace on top: multi-user accounts with bring-your-own-key support, an
in-browser code IDE with live sandboxed preview and local execution, Projects
for grouping related chats under shared instructions, and a task board for
running more than one build at a time.

Runs with **zero external setup** — no LLM API key, no Redis, no model
downloads — every real service (a provider, Redis, email) is one environment
variable away. See [OVERVIEW.md](OVERVIEW.md) for the fuller feature tour and
[SECURITY.md](SECURITY.md) for the account-security model.

**Contents:** [Architecture](#architecture) · [Quickstart](#quickstart) ·
[Accounts, BYO keys & sync](#accounts-byo-keys--sync) ·
[Projects](#projects) · [Web UI](#web-ui) · [API](#api) ·
[Code IDE + local execution](#code-ide--local-execution) ·
[Task board](#task-board) · [Results](#results--real-groq-llama-31-8b-instant)

## Architecture

```
                        POST /generate
                             │
                   ┌─────────▼──────────┐
   429 ◀───────────│  token-bucket rate │   (Step 5)
                   │  limiter           │
                   └─────────┬──────────┘
                             ▼
              ┌──────── exact cache (SHA256) ────────┐  hit → return  (Step 3)
              │              miss                    │
              ▼                                      │
   ┌── semantic cache (cosine ≥ threshold) ──┐  hit → return          (Step 4)
   │              miss                        │
   ▼                                          │
 circuit breaker → retry/backoff → LLM call ──┘  store in both caches  (Step 5)
   │                                   │
   └── open → 503                 mock (default) / groq / openai

 metrics: GET /metrics (Prometheus)   stats: GET /stats                (Step 7)
```

- **LLM backend** — `mock` by default (deterministic, offline, imitates API
  latency). `LLM_BACKEND=groq` + `LLM_API_KEY` for real calls.
- **Exact cache** — Redis if reachable, else in-process TTL dict.
- **Semantic cache** — `lexical` hashed-vector embedder by default (offline, no
  download); `EMBED_BACKEND=sbert` for real `all-MiniLM-L6-v2` paraphrase matching.
- **Resilience** — token-bucket limiter, exponential-backoff retries, circuit
  breaker — all dependency-free.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m scripts.baseline          # Step 1 — uncached baseline
uvicorn app.main:app --port 8000    # Steps 2-7 — the gateway
# in another terminal:
python -m scripts.demo              # exact-cache speedup
python -m scripts.loadtest          # 3-config comparison table (Step 6)
python -m scripts.resilience_check  # Step 5 primitives (no server needed)
python -m scripts.report            # before/after table from the request log
pytest                              # test suite (mock backend, no key needed)
```

Or run it containerised with a real, persistent Redis:

```bash
docker compose up --build           # gateway + Redis → http://localhost:8000
```

## Streaming, tests, logging, Docker

- **Streaming** — `POST /generate/stream` returns Server-Sent Events; the UI
  renders tokens as they arrive. Cache hits emit instantly as one frame. Rate
  limiting + circuit breaking still apply (mid-stream retries don't — a partial
  stream can't be replayed, so `/generate` stays the fully-resilient path).
- **Request log** — every call is appended to a SQLite log
  ([app/reqlog.py](app/reqlog.py)) with result / tokens / latency / cost;
  `scripts/report.py` turns it into a real before/after table, and
  `GET /log/summary` exposes the live aggregate.
- **Tests + CI** — `pytest` suite ([tests/](tests)) covering cache, semantic
  cache, resilience primitives, and the API (including streaming), all on the
  offline mock backend. [GitHub Actions](.github/workflows/ci.yml) runs it on
  Python 3.9/3.11/3.12.
- **Docker** — [Dockerfile](Dockerfile) + [docker-compose.yml](docker-compose.yml)
  run the gateway alongside a real Redis (append-only, persisted in a volume),
  so the exact cache survives restarts. Defaults stay offline; set
  `LLM_BACKEND`/`LLM_API_KEY` in a `.env` next to the compose file for real calls.
- **Observability stack** — the same `docker compose up` also starts
  **Prometheus** (scrapes `/metrics` every 5s) and **Grafana** with a
  pre-provisioned datasource + dashboard ([monitoring/](monitoring)) showing
  cache hit rate, request rate by result, p50/p95 latency, and errors:
  - Gateway → http://localhost:8000
  - Prometheus → http://localhost:9090
  - Grafana → http://localhost:3000 (dashboard "LLM Gateway"; anonymous view
    enabled, admin login `admin`/`admin`)

## Accounts, BYO keys & sync

The gateway grew from a single-user tool into a real multi-user app. With `AUTH_REQUIRED=1` (the default):

- **Accounts** — signup/login/logout, email verification (best-effort SMTP, printed to server logs when unconfigured), forgot/reset password, change password, sign out everywhere, and self-serve account deletion — all in Settings.
- **Bring your own key** — add a provider's API key in Settings → Connections to unlock its models; billed to you, not the app. The key is validated with a real (cheap, 1-token) call to the provider *before* it's saved — a bad key is rejected immediately with a clear error, never silently stored. Requires `APP_ENCRYPTION_KEY` (a Fernet key) to be set; BYO-key storage is refused outright otherwise, rather than ever falling back to plaintext.
- **Free daily quota** — `FREE_DAILY_TOKEN_LIMIT` per user per day (UTC), charged only on real, uncached calls using the app's own key — a BYO-key call or a cache hit never counts against it. `GET /usage/free` reports used/remaining.
- **Conversation sync** — a signed-in user's chat history is pushed/pulled from the server (`/sync/store`), so it follows them across browsers and devices instead of living only in one `localStorage`.
- **Invite-gated signup** (optional) — `SIGNUP_REQUIRES_INVITE=1` + `scripts/create_invite.py` if you don't want open public signup.

See [SECURITY.md](SECURITY.md) for the account-security model (password hashing, session handling, token lifetimes) and a deployment checklist.

## Projects

A persistent container for shared context across many separate chats —
distinct from a Code tab's own multi-file project, and kept deliberately
simple: Projects are for chats, Code tabs are their own thing.

- **Instructions** — free-text guidance applied to every chat inside the
  project (sent as a `system` turn ahead of that chat's own history).
- **Reference files** — plain-text files read into every chat in the
  project, the same way a one-off attachment is folded into a single
  message, just persistent across all of them.
- Chats in a project carry a small tag in the sidebar; deleting a project
  never deletes its chats — they're just unlinked back to normal.

## Multi-provider models

The gateway routes to any OpenAI-compatible provider — a model is just a base URL
+ key + model name ([app/config.py](app/config.py) `PROVIDERS` + `MODEL_CATALOG`).
`GET /models` lists the catalog and which providers have a key configured; the UI
shows a **model dropdown** (key-gated providers appear disabled as "needs key").
Requests carry an optional `model` (catalog id); the **cache key includes the
model**, so switching models never returns another model's cached answer, and
non-default models bypass the semantic cache. Add a provider by dropping its key
in the environment (`GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `MISTRAL_API_KEY`, …).

## Web UI

With the server running, open **http://localhost:8000** for a single-page
dashboard ([app/static/index.html](app/static/index.html), served at `GET /`):

- **Live metrics** — a floating circle (bottom-right) shows the live hit-rate
  at a glance; click it for a popup with the full dashboard (hit-rate gauge,
  API calls avoided, latency saved, tokens served from cache, circuit-breaker
  state, and a live/cache latency comparison). Keeps the chat area clutter-free.
- **Streaming answers** — responses stream in token-by-token via SSE; a
  "streaming…" indicator shows while they arrive, cache hits appear instantly.
- **Cache-tagged responses** — every answer is labelled MISS (live call),
  EXACT HIT, or SEMANTIC HIT (with similarity score). The full breakdown
  (server latency, round-trip, tokens, model, copy button) is tucked behind a
  **details** toggle so the feed stays clean; click to expand per message.
- **Tokens left (live)** — the header badge shows remaining tokens and
  **auto-refreshes every 3s** (and after each request); it turns red under 15%
  left. Click it for a live popover (remaining tokens/requests + reset timers,
  with a pulsing "live" dot) read from the provider's rate-limit response
  headers via `GET /quota`.
- **Per-model usage & cost** — for providers that don't expose live quota
  headers (Gemini, OpenRouter, …), the same popover shows tokens used,
  requests, and estimated cost for whichever model is selected. Backed by
  `GET /log/usage` (the SQLite request log), so it's durable across restarts
  and consistent across browsers/devices — not just a per-browser tally.
- **Conversation search** — a search box above the chat list filters by title
  or message content.
- **Edit & resend** — hover a past message for a ✎ button; editing it
  truncates the thread from that point and re-sends (no branching — keeps
  conversation memory consistent).
- **Graceful errors** — rate limits and transient failures (429/502/5xx/
  timeout) auto-retry with a visible countdown; permanent failures (no key,
  circuit open) show a friendly message with a one-click **Retry**, raw detail
  tucked behind **details**.
- **Drag-and-drop & paste** — drop an image/file anywhere in the chat, or
  paste one into the composer, to attach it (not just via the **+** menu).
- **Settings panel** — theme (system/light/dark), cursor-glow, backend/cache
  info, and cache reset are folded into one gear-icon popover; the composer
  itself stays down to model pill · input · send, with Memory/Code tucked
  behind a **⋯** options menu.
- **Keyboard shortcuts** — press **?** (or use the command palette) for a
  cheatsheet of everything above.
- **Attachments** — the **+** button adds a **Photo/Image**, a **camera** shot
  (live `getUserMedia` capture, with a file-input fallback), or a **File**:
  - Images route to a **vision model** (`config.LLM_VISION_MODEL`, default
    `qwen/qwen3.6-27b` on Groq) — genuine image understanding. Images are
    downscaled client-side (≤1024px) before upload.
  - Text/code files (`.txt .md .csv .json .py .js …`) are read in-browser and
    **PDFs** are sent as base64 and text-extracted server-side (pypdf), then
    folded into the prompt. Other binaries (docx/xlsx) are flagged unsupported;
    scanned/image-only PDFs (no embedded text) are flagged as needing OCR.
  - Cached by prompt **+ file contents + image hash**, so re-asking about the
    same image is an instant hit (a ~800 ms / 1.4k-token vision call → <1 ms).
    Requests with attachments skip the *semantic* cache (embedding the text
    while ignoring the image would risk false hits).
- **Conversations** — a sidebar to open new chats and switch back to old ones;
  history persists in the browser (`localStorage`). Each request is still a
  single-turn, independently-cached call — chats are UI groupings, not context
  threads — which keeps the caching behaviour honest.
- **Responsive** — the chat is usable end-to-end on a phone (drawer sidebar,
  wrapping composer, on-screen popovers); the code IDE degrades to a single
  stacked pane (editor + dock) below 760px.

No build step; it talks to whichever backend the server runs (`mock` or `groq`).

## API

| Method | Path            | Purpose                                                     |
|--------|-----------------|-------------------------------------------------------------|
| POST   | `/generate`     | `{"prompt", "images"[], "attachments"[{name,text}]}` → cached-or-fresh |
| POST   | `/generate/stream` | same body → **SSE** token stream (cache hits emit instantly)      |
| GET    | `/log/summary`  | aggregated request log (hit rate, tokens, cost by result)   |
| GET    | `/log/usage`    | per-model token/request/cost totals — durable, cross-browser |
| POST   | `/generate?cache_mode=none\|exact\|semantic` | override caching per request  |
| GET    | `/stats`        | live hits (exact/semantic) / misses / hit-rate / circuit    |
| GET    | `/quota`        | latest provider rate-limit snapshot (tokens/requests left)  |
| GET    | `/metrics`      | Prometheus exposition (Step 7)                              |
| POST   | `/admin/reset`  | clear caches + counters (used by the load test) — **loopback-only** |
| GET    | `/healthz`      | liveness                                                    |
| GET    | `/run/env`      | which language runtimes are installed on this machine       |
| POST   | `/run/start`    | start a local run of a project's files → `{session}`        |
| GET    | `/run/stream/{id}` | **SSE** stdout/stderr of a run session                   |
| POST   | `/run/input`    | feed a line to a running process's stdin (interactive)      |
| POST   | `/run/kill`     | kill a running session                                      |
| GET    | `/workspace/list` · `/workspace/file` | list / fetch files a run kept in its workspace |
| POST   | `/workspace/commit` · `/workspace/restore` · GET `/workspace/history` | git-snapshot / restore / list a run workspace's history |
| POST   | `/lsp/python`   | Jedi Python IntelliSense (complete / hover / definition), loopback-only |
| POST   | `/publish`      | stitch a project into one HTML doc, store it under a slug → `{slug, url}` |
| GET    | `/p/{slug}`     | serve a published page (locked-down CSP — see SECURITY.md) |
| POST   | `/auth/signup` · `/auth/login` · `/auth/logout` | account creation / session — **rate-limited** |
| GET    | `/auth/me`      | current session's user, or `null`                            |
| POST   | `/auth/verify-email` · `/auth/resend-verification` | email verification flow |
| POST   | `/auth/change-password` · `/auth/sign-out-everywhere` · `/auth/delete-account` | require re-entering the current password |
| POST   | `/auth/forgot-password` · `/auth/reset-password` | email-based reset — **rate-limited**, single-use token |
| GET/POST | `/keys`, `DELETE /keys/{provider}` | manage BYO provider keys — key is validated with a real call before saving |
| GET    | `/usage/free`   | this user's free-tier usage today (used/limit/remaining)   |
| GET/POST | `/sync/store` | pull/push this user's synced conversation history           |

## Code IDE + local execution

The **Code** tab in the web UI is a real in-browser IDE built on **Monaco** (the
editor core behind VS Code), vendored locally under `app/static/vendor/monaco`
and served at `/vendor` so it stays **fully offline** — no CDN. Multi-file
projects (file tabs), multi-cursor, minimap, format, and a live **sandboxed**
web preview that auto-runs HTML/CSS/JS, captures the console, and offers
**Fix it with AI**.

**Language intelligence** (completions, hover, diagnostics, go-to-definition):

- **JS / TS / CSS / HTML / JSON** — Monaco's built-in language services.
- **Python** — a Jedi-backed endpoint (`POST /lsp/python`, loopback-guarded;
  needs the `jedi` dependency) wired to Monaco's completion / hover / definition
  providers. Static analysis only — it never runs the code.

**Find** — in-file is Monaco's native `Ctrl/Cmd+F`; **project-wide search** is
`Cmd+Shift+F` (or the "Search across files" command-palette entry), which walks
every file and jumps across them.

**Runtime-error markers** — a local run's real traceback (Python, Node, …) is
parsed and shown as an inline Monaco marker on the failing line — red squiggle,
gutter icon, exact exception on hover — cleared automatically on the next run.
(Browser-sandbox errors stay console-only + click-to-jump, since their line
numbers are in an instrumented, stitched document that doesn't map cleanly to
source.)

Beyond the browser sandbox, it can **run real scripts on this machine** —
Python, Node, Bash, Ruby, and more — with interactive stdin, `pip`/`npm`
dependency installs (isolated per workspace, no global pollution), and a
**persistent per-project workspace** whose files survive between runs and are
listed/downloadable in the **Files** tab.

**Agentic build loop** — "Agent: build & fix until it runs" (command palette)
takes a goal from the chat box and loops **generate → write files → run
locally → read the real exit code/output → re-prompt with the error → repeat**
until the program exits 0 or the iteration cap is hit. Targets local-runnable
script projects (a nonzero exit code is an objective success signal — a web
UI has no automatic pass/fail, so the loop doesn't apply there). Each
iteration is a real, uncached LLM call, so a fix attempt can't just replay a
stale cached answer.

**Security.** This executes arbitrary code with the server's privileges, so by
default `/run/*` and `/workspace/*` accept **loopback clients only** — remote
requests get `403` even if you bind `0.0.0.0`. Override knobs
([app/config.py](app/config.py)):

- `ENABLE_LOCAL_RUN=0` — turn local execution off entirely.
- `RUN_TOKEN=…` — require an `X-Run-Token` header on run/workspace calls.
- `RUN_ALLOW_REMOTE=1` — allow non-local clients (only behind your own auth).

Guardrails: isolated working dir, wall-clock timeout that kills the whole
process group, and a 2 MB output cap for runaway loops.

## Build mode extras: trace, checkpoints, publish, screenshots

- **Build trace** — a Build's chat message shows a live pipeline (*writing
  code → running & checking → ✓ runs clean / ⚠ N errors*) instead of going
  silent between "done generating" and the pass/fail chip landing.
- **Checkpoints** — every message that changed the project carries a full
  snapshot of its files at that point (`msg.projectSnapshot`); a **↺ Revert
  to here** action on the message restores exactly that state, independent
  of any edits made since.
- **Publish** — `POST /publish` stitches the current project into one HTML
  document and stores it under a random slug; `GET /p/{slug}` serves it as a
  standalone, shareable page (no third-party deploy account, no expiry) —
  see [SECURITY.md](SECURITY.md) for the CSP that isolates it from the app's
  own session.
- **Screenshot / design → code** — attach an image while Build mode is on
  and the code-mode system prompt treats it as a layout reference to
  reproduce, not just something to describe.

## Task board

Every **Code** tab is a task. The board (a "⊞" button next to "Code tabs" in
the sidebar) is a Kanban view over all of them:

- **Columns** (Not started / In progress / Needs fix / Done) are live-derived
  from each task's actual status — the same signal behind the sidebar's
  status dots — unless manually overridden by dragging a card, which pins it
  until reset via "↺ auto".
- **Goal notes** — a short, inline-editable description per task, separate
  from its chat title.
- **Real background concurrency** — the send/stream pipeline tracks an
  `AbortController` per conversation (not one global one), so a Build can
  keep streaming in one tab while you work in another. The live-preview
  check itself also runs in the background for a finished web project, in a
  disposable, invisible `<iframe>` isolated from whatever's on screen — only
  local-script execution still waits for you to switch back to that tab,
  deliberately (a real interpreter running unattended for a tab nobody's
  watching is a different risk than sandboxed JS nobody can see).
- **Fork & merge** — "⑂ Fork" clones a task's project into a new,
  independently-running linked task (`taskGroup`); "⇄ Merge" diffs two
  linked tasks' files and lets you pick "keep mine" / "keep theirs" per
  changed file before applying.

## Results — real Groq (`llama-3.1-8b-instant`)

Measured against the live Groq API on this machine.

**Cache hit vs. miss latency** (demo, repeated traffic, 75% hit rate):

| metric              | value              |
|---------------------|--------------------|
| avg MISS (real API) | **748 ms**         |
| avg HIT (cache)     | **< 1 ms**         |
| speedup on hits     | ~1000×+            |

**Uncached baseline** (5 varied prompts): avg **756 ms/call**, 1464 tokens total.

**Caching comparison** (22-request mix of repeats / near-duplicates / one-offs,
each config started cold via `/admin/reset`):

| config    | hit rate | billed tokens | vs. no-cache   |
|-----------|----------|---------------|----------------|
| no cache  | 0%       | 4919          | —              |
| exact     | 36%      | 3209          | 35% fewer tok  |
| semantic  | 77%      | 1403          | **71% fewer tok** |

**Semantic caching cut billed tokens 71% and turns 748 ms API calls into
sub-millisecond cache hits.** It beats exact-match by catching near-duplicate
phrasings ("capital of France?" vs "What is the capital of France?").

> Honest caveats: the per-config *latency under concurrent load* is noisy on
> Groq's free tier — bursts trip Groq's own rate limit, so our retry/backoff
> adds wait time (that's the circuit-breaker/retry logic working, not a cache
> effect). The clean hit-vs-miss latency above comes from the low-concurrency
> demo. Token-reduction and hit-rate are the robust, provider-independent wins.
> The mock backend (`LLM_BACKEND=mock`, the default) reproduces the same hit
> rates offline with simulated latency.

### Reproduce

```bash
python -m scripts.baseline                              # baseline
uvicorn app.main:app --port 8000                        # server (terminal 1)
python -m scripts.demo                                  # hit vs miss latency
LT_CONCURRENCY=2 LT_SCALE=0.25 python -m scripts.loadtest   # gentle on free tier
```

To switch backends: `.env` → `LLM_BACKEND=groq` + `LLM_API_KEY=gsk_your_key_here`.
Optional upgrades (same pattern): `REDIS_URL=...` for real Redis, `EMBED_BACKEND=sbert`
(+ `pip install sentence-transformers`) for true paraphrase matching.

## Build-plan status

- [x] Step 1 — uncached baseline (`scripts/baseline.py`)
- [x] Step 2 — FastAPI `/generate` (`app/main.py`)
- [x] Step 3 — Redis exact-match cache, in-memory fallback (`app/cache.py`)
- [x] Step 4 — semantic cache, lexical + sbert embedders (`app/semantic_cache.py`, `app/embeddings.py`)
- [x] Step 5 — rate limiting + retry/backoff + circuit breaker (`app/resilience.py`)
- [x] Step 6 — load test across 3 cache configs (`scripts/loadtest.py`, `locustfile.py`)
- [x] Step 7 — Prometheus metrics + `/stats` (`app/metrics.py`)

For Grafana, point it at `GET /metrics`; the counters (`gateway_requests_total`
by result, `gateway_request_latency_seconds`) are ready to graph.
