"""Generate a signup invite code (hybrid build, Phase 4).

Only meaningful when SIGNUP_REQUIRES_INVITE=1 — deliberately a script, not an
HTTP endpoint, so granting invites always requires shell access to the server,
not just a browser.

Run:  python -m scripts.create_invite
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store  # noqa: E402

if __name__ == "__main__":
    store.init_db()
    code = store.create_invite_code()
    print(f"New invite code: {code}")
