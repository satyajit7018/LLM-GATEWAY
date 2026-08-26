# Security

## Reporting

This is a personal/self-hosted project, not a maintained public service — there's no bug bounty or disclosure program. If you're running your own instance and find something, fix it directly; the notes below exist so you know what to check.

## What's already in place

- **Passwords** — PBKDF2-HMAC-SHA256, 240,000 iterations, per-user random salt (`app/store.py`).
- **Sessions** — random opaque tokens (`secrets.token_urlsafe(32)`); only a SHA-256 hash is stored, so a DB leak doesn't hand out live sessions. Cookies are `HttpOnly`, `SameSite=Lax`, and `Secure` when `COOKIE_SECURE=1` (see the deployment checklist below).
- **BYO provider keys** — encrypted at rest with Fernet (`APP_ENCRYPTION_KEY`); BYO-key storage is refused outright if that key isn't configured, rather than ever falling back to storing keys unencrypted. A key is validated with a real test call to the provider before it's ever saved.
- **Password reset / email verification** — single-use, expiring tokens (1h / 24h), hashed the same way as session tokens. A password reset signs out every other session for that account.
- **Sensitive account actions** (change password, delete account) — require re-entering the current password, so a hijacked session token alone can't do either.
- **No user enumeration** — `/auth/forgot-password` always returns the same generic response whether or not the email is registered.
- **Local code execution** (`/run/*`, `/workspace/*`) — loopback-only by default (`ENABLE_LOCAL_RUN`, `RUN_ALLOW_REMOTE`, `RUN_TOKEN` in `app/config.py`); a wall-clock timeout kills the whole process group, output is capped, and each project runs in its own isolated working directory.
- **Published pages** (`/p/{slug}`) — served with `Content-Security-Policy: connect-src 'none'; form-action 'none'; frame-ancestors 'none'`, so a published page's own script can render and be interactive, but can't fetch/XHR back into the app's API or submit a form anywhere, using the visiting browser's session.

## Findings from the pre-launch security review, and their fixes

Run once the app moved from a single-user gateway to real multi-user accounts + BYO keys — that's a materially different risk profile, so it got a dedicated pass rather than assuming the original threat model still covered it. All three are fixed and covered by `tests/test_security_review.py`, not just reported:

1. **`/admin/reset` was public and unauthenticated.** Anyone could wipe the shared cache and counters with one POST. Now restricted to loopback clients, the same way local code execution already was.
2. **No rate limiting on auth endpoints.** `/auth/login`, `/auth/signup`, `/auth/forgot-password`, and `/auth/resend-verification` had no throttling — unlimited password-guessing against a known email, and forgot/resend could be used to spam a victim's inbox. They now share one tight, IP-keyed token bucket (`AUTH_RATE_LIMIT_RPS` / `AUTH_RATE_LIMIT_BURST`), separate from the generation rate limiter.
3. **Published pages had no isolation.** `/publish` doesn't require being signed in, and `/p/{slug}` served the resulting HTML/JS at the app's own origin with no CSP — a malicious published page's script could otherwise ride an already-logged-in visitor's session cookie to call the app's own API as them (same-origin `fetch`/`XHR` isn't blocked by `SameSite`). Fixed with the CSP above.

## Deployment checklist

None of this is enforced automatically — these are things to actually set before exposing an instance beyond your own machine:

- [ ] **`COOKIE_SECURE=1`** — off by default so local HTTP dev works; without it in production, the session cookie can be sent over plain HTTP.
- [ ] **`APP_ENCRYPTION_KEY`** — a real Fernet key (`Fernet.generate_key()`), kept out of source control, if you want BYO-key storage enabled at all.
- [ ] **`AUTH_REQUIRED=1`** — the default; only turn it off for a purely local/offline single-user setup.
- [ ] Leave **`RUN_ALLOW_REMOTE`** unset — local code execution should stay loopback-only unless you've put your own auth in front of it.
- [ ] Consider **`SIGNUP_REQUIRES_INVITE=1`** (with `scripts/create_invite.py`) if you don't want open public signup.
- [ ] Rotate `APP_ENCRYPTION_KEY` only with a migration plan — existing BYO keys become undecryptable (and are treated as absent, not crashing) if the key changes without one.

## What this review didn't cover

This was a review of the app's own code, not a full penetration test or dependency audit. It didn't include: a `pip`/`npm` dependency vulnerability scan, load-testing the rate limiters under real adversarial traffic, or a review of the Docker/Compose deployment's own hardening (running as non-root, image scanning, etc.). Worth doing before running this for anyone other than yourself at real scale.
