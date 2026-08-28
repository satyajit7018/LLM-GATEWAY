# Hybrid multi-user build — phased plan

Building the hybrid model (accounts + free default models + bring-your-own-key
premium) **across separate sessions** to conserve tokens. Resume with the
**`/hybrid`** shortcut. Do ONE phase per session.

## Status
- [x] **Phase 1 — Auth foundation** ✅ DONE — `app/store.py` (users/sessions,
      PBKDF2, opaque sessions), `/auth/{signup,login,logout,me}`, `require_user`
      gate on `/generate` + `/generate/stream`, login/signup UI gate, Settings
      sign-out, per-user localStorage namespacing. 42 tests pass (6 new auth).
- [x] **Phase 2 — BYO keys + model gating** ✅ DONE — `user_keys` table
      (Fernet-encrypted via `APP_ENCRYPTION_KEY`; disabled with a clear error if
      unset), `GET/POST/DELETE /keys`, `/models` now reports `source`
      (`"app"`/`"user"`/`null`) per model, `/generate`+`/generate/stream`
      resolve the user's own key first (falls back to the app key), a user's
      quota/rate-limit headers no longer pollute the shared app-level display.
      Frontend: Settings → **Connections** (add/remove keys, masked
      `••••last4`); locked models auto-unlock in the picker the moment a key
      is added. Found + fixed a real bug along the way: `/keys` must always
      require a *real* session regardless of the `AUTH_REQUIRED` toggle (added
      `require_real_user`, separate from the soft `require_user` used by
      `/generate`). 53 tests pass (11 new).
- [x] **Phase 3 — Free-tier per-user quotas** ✅ DONE — `usage(user_id, day,
      tokens, requests)` table (resets at UTC midnight, no cron needed),
      `FREE_DAILY_TOKEN_LIMIT` (default 50,000; 0 = unlimited). Quota is
      checked before any upstream call and charged only after a real
      (uncached) app-key call — cache hits and BYO-key calls never count
      against it. `GET /usage/free` reports used/limit/remaining. Frontend:
      a "Free tier today" row + bar in Settings, live-updating after each
      real message; the frontend's error classifier now special-cases the
      quota 429 as non-retryable (the generic 429 handler would otherwise
      auto-retry a wall that retrying can't get past). 62 tests pass (9 new).
      Verified live end-to-end incl. real exhaustion (`FREE_DAILY_TOKEN_LIMIT=1`
      → 2nd call correctly 429s with no wasted retry countdown).
- [x] **Phase 4 (optional) — server-side conversation sync; email/invite; reset**
      ✅ DONE (all three sub-features, user chose all of them):
      - **Invite-gated signup** — `invite_codes` table, `SIGNUP_REQUIRES_INVITE`
        (off by default), `scripts/create_invite.py` (deliberately a script,
        not an HTTP admin endpoint). Redemption is atomic; a code that fails
        to redeem rolls back the just-created user row (no orphaned accounts).
      - **Password reset via email** — `password_resets` table (single-use,
        1h TTL), `app/mailer.py` (real SMTP via stdlib `smtplib` if configured,
        else logs the link to the server console — never fabricated).
        `/auth/forgot-password` gives an identical response whether or not the
        email exists (no user-enumeration). Resetting invalidates all of that
        user's existing sessions. Frontend: "Forgot password?" link, and a
        `?reset_token=` URL param opens the new-password view directly.
      - **Server-side conversation sync** — `user_store` table holds each
        user's whole chat-store JSON blob (same shape as localStorage, so the
        frontend diff stayed minimal). `GET/PUT /sync/store`. On login, pulls
        the server copy if one exists (or seeds the server from local on first
        login); `save()` now also debounce-pushes. localStorage stays as the
        fast local cache.
      86 tests pass (12 new). Verified live end-to-end for all three,
      including real cross-device sync (fresh localStorage on the same
      account correctly pulled the other "device"'s conversation) and a full
      forgot→email-logged→reset→re-login-with-new-password round trip.
      **Found + fixed a real bug**: `.auth-tabs`/`#auth-form` each had their
      own `display: flex`, which (via CSS specificity) silently overrode the
      browser's default `[hidden] { display: none }` — so switching to the
      forgot/reset views left the login form visibly stacked on top instead of
      replacing it. Fixed with explicit `[hidden]` overrides.

- [x] **Phase 5 — Motion, Aesthetics & Visual Polish** ✅ DONE:
      - **Pixar-Grade Companion Widget:** Overhauled 17 CSS keyframes with 60fps GPU transforms, non-dipping baseline positioning (`bottom: 100%; margin-bottom: 2px`), upright typing & snacking choreography, thought bubble dialogue system, and reactive Code Mode developer headphones.
      - **Glassmorphism Design System:** Consistent translucent surfaces with `backdrop-filter: blur(16px)` across all popovers (`.settings-pop`, `.metrics-pop`, `.quota-pop`, `.acct-menu`).
      - **Interactive Empty-State Starters:** 1-click prompt cards for rapid prototyping across chat and Code Mode tabs.
      - **Glassmorphic Toast Engine:** Non-blocking notification stack (`showToast()`) with progress timer bars and auto-dismissal.
      - **Tactile Micro-Interactions:** Universal `scale(0.96)` active-press physics across all buttons, chips, and cards; organic spring-eased message bubbles (`@keyframes msgRise`).
      - **Full-Screen Multi-Modal Dropzone:** Frosted glass drag-and-drop overlay (`backdrop-filter: blur(20px)`) with drop counter for attaching images, PDFs, and code files.
      - **Streaming Markdown Auto-Closer:** Auto-detects and closes dangling triple-backtick code fences in real-time so syntax highlighting renders progressively while streaming.
      - **Persistent Sidebar Navigation:** Manually collapsed/expanded sidebar states persist across reloads via `localStorage`.
- [x] **Phase 6 — Quality, Tooling & Verification Audit** ✅ DONE:
      - Cleaned all unused imports across the codebase, declared explicit `__all__` exports in `app/main.py`.
      - Added `pyrightconfig.json` and `.vscode/settings.json` configuring language servers to use the project `.venv`.
      - Resolved cross-browser CSS `line-clamp: 2` compatibility and escaped embedded regex slashes in HTML pretty-printer.
      - Expanded test suite to **153 automated tests** (27 Playwright browser smoke tests + 126 backend/security tests) passing in ~30s.

## All phases complete
The hybrid multi-user gateway (accounts, BYO keys, quotas, conversation sync, interactive sandbox, and Pixar-grade visual companion) is 100% complete, verified, and audited.

## Post-Phase-4 fixes — real correctness gaps the multi-user transition left behind
Found via a self-review after Phase 4 ("what should a logged-in user have that's
missing"), then fixed:
- **Per-account rate limiting.** The original Step-5 rate limiter (`bucket` in
  `resilience.py`) is one GLOBAL `TokenBucket` shared by every request — with
  multiple accounts now, one busy user could 429 every other account by
  exhausting that shared budget alone. Added `KeyedTokenBuckets` (one bucket
  per logged-in user, or per client IP when nobody's logged in), checked
  *on top of* the existing global cap, not instead of it — the global bucket
  still protects the upstream provider in aggregate; the per-user one adds
  fairness between accounts. New config: `PER_USER_RATE_LIMIT_RPS`/`_BURST`
  (defaults 10/20, tighter than the global 50/100).
- **`/log/usage` was a whole-server aggregate, not scoped to the logged-in
  user.** `reqlog.py`'s `requests` table had no `user_id` column at all —
  confirmed against the real `requests.db` (108 pre-existing rows). Added the
  column with a safe migration (`ALTER TABLE` only if missing — verified the
  108 old rows survive with `user_id = NULL`, new rows get attributed).
  `/log/usage` now requires a real session (`require_real_user`, matching
  `/usage/free`/`/keys`/`/sync/store`) and filters to that user's own rows;
  `reqlog.summary()`/`/log/summary` (the cache-effectiveness ops report used
  by `scripts/report.py`) deliberately stayed global — that one's a legitimate
  operational metric, not per-user private data.
- 4 new tests (`tests/test_multiuser_fixes.py`), 78 total, stable across
  repeated runs. Verified live against the real Groq backend too: a brand-new
  second account correctly saw `null` usage instead of the first account's
  real token/cost data, both via `/log/usage` directly and through the actual
  Tokens-popover UI.
- **Test-writing lesson (hit twice now, worth remembering):** a "unique"
  prompt string that shares common words with another prompt (e.g. two prompts
  both starting "test message …") can register as a semantic-cache *near-duplicate*
  and get served from cache instead of exercising a fresh code path — the fix
  in `_fresh_prompt()` helpers across the test suite is to use a bare
  `uuid4().hex` with no natural-language wrapper at all.

## Assumptions (defaults chosen since deployment/signup were left open)
- **Mode:** local/demo. Auth is built production-shaped (httponly cookies,
  salted-hashed passwords) but cookie `secure` flag is env-controlled
  (`COOKIE_SECURE`, default off for http://localhost).
- **Signup:** open (per-user quota arrives in Phase 3).
- **Before any public hosting:** disable `/run` `/workspace` `/lsp` (RCE risk),
  turn on `COOKIE_SECURE`, serve over HTTPS. Not done yet — local mode.

---

## Phase 1 — Auth foundation  (this session)
**Goal:** accounts + sessions + a login gate. No keys yet; everything still uses
the app-owned provider keys.

- `app/store.py` — SQLite users + sessions tables; PBKDF2 password hashing
  (stdlib, no new dependency); opaque session tokens (stored hashed).
- `app/config.py` — `AUTH_REQUIRED` (default 1), `COOKIE_SECURE` (default 0),
  `AUTH_DB_PATH`.
- `app/main.py` — `current_user` / `require_user` deps; `POST /auth/signup`,
  `/auth/login`, `POST /auth/logout`, `GET /auth/me`; gate `/generate` +
  `/generate/stream` (+ `/models`, `/quota`, `/log/usage`) behind auth.
- `app/static/index.html` — full-screen login/signup gate shown when `/auth/me`
  is 401; "Sign out" in Settings; conversations `localStorage` namespaced by
  user id.
- Tests — `tests/conftest.py` sets `AUTH_REQUIRED=False` so existing gateway
  tests stay green; new `tests/test_auth.py` covers signup/login/me/logout.

## Phase 2 — BYO keys + gating
- `user_keys(user_id, provider, ciphertext, last4)`, Fernet encryption via
  `APP_ENCRYPTION_KEY` (`cryptography` dep). `GET/POST/DELETE /keys`.
- `/models` per-user: free models always; premium flagged locked/unlocked by
  whether the user has that provider's key.
- `/generate` resolves key: app key for free models, user key for premium.
- Frontend: Settings "Connections" (add/remove keys, masked `sk-…last4`);
  locked models show "add your key to unlock".

## Phase 3 — Free-tier quotas
- `usage(user_id, day, provider, requests, tokens)`; per-user daily cap on
  app-owned (free) models → 429 when exceeded; usage shown in UI.

## Phase 4 — optional
- Server-side conversation sync (off localStorage); email verification / invite
  codes; password reset.

## Post-Phase-4 account-management nice-to-haves
Found via the same "logged-in user" self-audit that produced the rate-limiter
and reqlog fixes above; built once the user picked which ones mattered:
- **Change password (while logged in).** `POST /auth/change-password`
  (`require_real_user`, re-checks the current password via
  `store.verify_user_password`). Keeps the session that made the change alive
  but signs every *other* session out (`store.delete_other_sessions`) — same
  defensive move as a reset, without logging out the device that just proved
  who it is.
- **Sign out everywhere.** `POST /auth/sign-out-everywhere` —
  `store.delete_all_sessions` plus clearing the cookie on this response too;
  "everywhere" means literally everywhere, this device included.
- **Account deletion.** `POST /auth/delete-account`, password-confirmed.
  `store.delete_account` removes sessions/keys/usage/synced-store/reset-tokens
  scoped to the user, then the user row itself. Deliberately leaves
  `invite_codes.used_by` pointing at the deleted id rather than nulling it —
  nulling would satisfy `used_by IS NULL` and let a spent code be redeemed
  again.
- **Email verification.** New `email_verified` column (migrated in for
  existing DBs) + `email_verifications` table, same shape/TTL pattern as
  password resets (24h, single-use, hashed token). Signup sends a
  verification link (`mailer.send_verification_email`, same
  SMTP-or-console-log fallback as reset emails); non-blocking — verification
  is informational only, nothing is gated on it. `POST /auth/verify-email` (no
  session required — the link is often opened on a different
  device/browser than the one that signed up), `POST /auth/resend-verification`
  (`require_real_user`). Frontend shows a "Not verified · Resend" row in
  Settings and reads `?verify_token=` off the URL at boot regardless of login
  state.
- **Found + fixed a real bug along the way**: `.set-row` (and, from this same
  change, `.set-actions`) set `display` directly, which — same as the
  documented `.auth-tabs`/`#auth-form` bug from Phase 4 — silently overrides
  the browser's default `[hidden] { display: none }` once a *class* selector
  ties its specificity. Every row that only ever goes hidden→shown once
  (acct-row, quota-row) masked this; the new verify-row is the first one that
  needs to hide *again* after being shown, which is what exposed it. Fixed
  with explicit `.set-row[hidden]`/`.set-actions[hidden]` overrides — same
  fix pattern as before, now worth grepping for on any future toggled `.set-*`
  element.
- 12 new tests (`tests/test_account_mgmt.py`), full suite stays green.
  Verified live: wrong-current-password and wrong-delete-password both
  surface correctly, a password change keeps the current session while
  killing a second real logged-in session, sign-out-everywhere logs out the
  calling session too, account deletion round-trips (old password rejected
  post-delete), and a real `?verify_token=` link (grabbed from the
  console-log fallback) verifies and updates the UI without a page reload.

## Commercial pivot
Separate from the build — see `docs/commercialization/strategy.md` (`/commercialize`).
