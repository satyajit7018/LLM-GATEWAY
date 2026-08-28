"""Regression tests for Operation CLEAR QUERY.

Covers:
- Q-01/02: user_for_session() returns None for expired tokens and eagerly deletes them
- Q-03:    user_id_for_reset_token() returns None for expired tokens
- Q-04:    verify_email_token() returns None for expired tokens
- Q-05:    idx_sessions_expires + idx_resets_expires exist after init_db()
- Q-06:    idx_requests_ts exists after reqlog._connect()
- purge:   purge_expired_sessions() removes only expired rows, leaves live ones intact

All tests call store/reqlog functions directly against the in-memory DB configured
in conftest.py — no HTTP layer involved, no file I/O.
"""
from __future__ import annotations

import time

import app.reqlog as reqlog
import app.store as store



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_user(suffix=""):
    """Create and return a minimal user dict for tests that need a real user_id."""
    email = f"query_fix_{suffix}_{time.time_ns()}@example.com"
    return store.create_user(email, "password123")


# ---------------------------------------------------------------------------
# Q-01 / Q-02 — user_for_session()
# ---------------------------------------------------------------------------

class TestSessionExpiry:
    def test_live_session_returns_user(self):
        u = _fresh_user("live")
        tok = store.create_session(u["id"])
        result = store.user_for_session(tok)
        assert result is not None
        assert result["id"] == u["id"]

    def test_expired_session_returns_none(self, monkeypatch):
        u = _fresh_user("exp")
        tok = store.create_session(u["id"])
        # Wind time forward past the session TTL by monkeypatching time.time
        far_future = time.time() + 60 * 60 * 24 * 31   # 31 days — past 30-day TTL
        monkeypatch.setattr(store.time, "time", lambda: far_future)
        assert store.user_for_session(tok) is None

    def test_expired_session_is_eagerly_deleted(self, monkeypatch):
        """After a failed lookup for an expired token, the row must be gone
        from the DB — not left as dead weight for a future purge to find."""
        u = _fresh_user("del")
        tok = store.create_session(u["id"])
        far_future = time.time() + 60 * 60 * 24 * 31
        monkeypatch.setattr(store.time, "time", lambda: far_future)
        # Trigger the lazy delete via a lookup
        store.user_for_session(tok)
        # Restore real time and check the row is gone
        monkeypatch.undo()
        tok_hash = store._tok_hash(tok)
        with store._lock:
            conn = store._connect()
            row = conn.execute(
                "SELECT 1 FROM sessions WHERE token_hash = ?", (tok_hash,)
            ).fetchone()
        assert row is None, "expired session row should have been deleted on lookup"

    def test_unknown_token_returns_none(self):
        assert store.user_for_session("this-token-does-not-exist") is None


# ---------------------------------------------------------------------------
# purge_expired_sessions()
# ---------------------------------------------------------------------------

class TestPurgeExpiredSessions:
    def test_purge_removes_expired_leaves_live(self, monkeypatch):
        u = _fresh_user("purge")
        live_tok = store.create_session(u["id"])

        # Create a session then fast-forward to expire it
        expired_tok = store.create_session(u["id"])
        far_future = time.time() + 60 * 60 * 24 * 31
        monkeypatch.setattr(store.time, "time", lambda: far_future)
        # Run purge while time is in the future (so both rows look expired from purge's POV)
        # — restore first so only the one we created pre-future actually expired
        monkeypatch.undo()

        # Re-create expired session manually with a past expires_at
        expired_hash = store._tok_hash(expired_tok)
        with store._lock:
            conn = store._connect()
            conn.execute(
                "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
                (time.time() - 1, expired_hash),
            )
            conn.commit()

        store.purge_expired_sessions()

        # Live session still exists
        assert store.user_for_session(live_tok) is not None
        # Expired session is gone
        with store._lock:
            conn = store._connect()
            gone = conn.execute(
                "SELECT 1 FROM sessions WHERE token_hash = ?", (expired_hash,)
            ).fetchone()
        assert gone is None, "purge should have deleted the expired session row"


# ---------------------------------------------------------------------------
# Q-03 — user_id_for_reset_token()
# ---------------------------------------------------------------------------

class TestPasswordResetTokenExpiry:
    def test_live_reset_token_returns_user_id(self):
        u = _fresh_user("rst_live")
        tok = store.create_reset_token(u["id"])
        assert store.user_id_for_reset_token(tok) == u["id"]

    def test_expired_reset_token_returns_none(self, monkeypatch):
        u = _fresh_user("rst_exp")
        tok = store.create_reset_token(u["id"])
        far_future = time.time() + 60 * 70   # 70 minutes — past 1-hour TTL
        monkeypatch.setattr(store.time, "time", lambda: far_future)
        assert store.user_id_for_reset_token(tok) is None

    def test_consumed_reset_token_returns_none(self):
        u = _fresh_user("rst_used")
        tok = store.create_reset_token(u["id"])
        store.consume_reset_token(tok)
        assert store.user_id_for_reset_token(tok) is None


# ---------------------------------------------------------------------------
# Q-04 — verify_email_token()
# ---------------------------------------------------------------------------

class TestEmailVerificationTokenExpiry:
    def test_live_verification_token_marks_user_verified(self):
        u = _fresh_user("ev_live")
        tok = store.create_verification_token(u["id"])
        returned_id = store.verify_email_token(tok)
        assert returned_id == u["id"]
        assert store.is_email_verified(u["id"])

    def test_expired_verification_token_returns_none(self, monkeypatch):
        u = _fresh_user("ev_exp")
        tok = store.create_verification_token(u["id"])
        far_future = time.time() + 60 * 60 * 25   # 25 hours — past 24-hour TTL
        monkeypatch.setattr(store.time, "time", lambda: far_future)
        assert store.verify_email_token(tok) is None
        # User must not be marked verified if the token was expired
        monkeypatch.undo()
        assert not store.is_email_verified(u["id"])

    def test_already_used_verification_token_returns_none(self):
        u = _fresh_user("ev_used")
        tok = store.create_verification_token(u["id"])
        store.verify_email_token(tok)   # consume it
        assert store.verify_email_token(tok) is None   # second use must fail


# ---------------------------------------------------------------------------
# Q-05 — Index existence (sessions + password_resets)
# ---------------------------------------------------------------------------

class TestStoreIndexes:
    def _index_names(self, table: str) -> set[str]:
        with store._lock:
            conn = store._connect()
            rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
        return {r[1] for r in rows}

    def test_idx_sessions_user_exists(self):
        assert "idx_sessions_user" in self._index_names("sessions")

    def test_idx_sessions_expires_exists(self):
        assert "idx_sessions_expires" in self._index_names("sessions")

    def test_idx_resets_expires_exists(self):
        assert "idx_resets_expires" in self._index_names("password_resets")


# ---------------------------------------------------------------------------
# Q-06 — Index existence (reqlog)
# ---------------------------------------------------------------------------

class TestReqlogIndexes:
    def _index_names(self, table: str) -> set[str]:
        with reqlog._lock:
            conn = reqlog._connect()
            rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
        return {r[1] for r in rows}

    def test_idx_requests_user_exists(self):
        assert "idx_requests_user" in self._index_names("requests")

    def test_idx_requests_ts_exists(self):
        assert "idx_requests_ts" in self._index_names("requests")
