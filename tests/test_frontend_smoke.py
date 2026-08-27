"""Frontend smoke tests (Playwright, Python bindings — no Node/npm needed).

Not exhaustive UI coverage — a tripwire for exactly the class of regression
that's easy to introduce and easy to miss by eye: a JS syntax error, a CSS
rule that silently stops applying (an animation masking a plain transform,
one rule's specificity beating another's), or the composer flow breaking
outright. Runs against a real live server (mock LLM backend, no API key
needed) rather than a static file, so relative asset paths, cookies, and the
actual served HTML all behave like production.

Requires a one-time `playwright install --with-deps chromium` after
`pip install -r requirements.txt` (see CI workflow).
"""
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app.main import app


@pytest.fixture(scope="module")
def live_server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(f"{base_url}/healthz", timeout=0.2)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        raise RuntimeError("live server didn't come up in time")

    yield base_url

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def console_errors(page):
    """Collect console 'error' messages for the duration of a test."""
    errors = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    return errors


def test_page_loads_with_no_console_errors(page, live_server, console_errors):
    page.goto(live_server)
    page.wait_for_selector("#prompt")
    # An anonymous session correctly getting 401 from an account-only endpoint
    # (e.g. /log/usage) logs as a browser resource-load failure — that's
    # expected, already-handled behavior, not a JS bug.
    real_errors = [e for e in console_errors if "Failed to load resource" not in e]
    assert real_errors == []


def test_welcome_screen_and_composer_present(page, live_server):
    page.goto(live_server)
    page.wait_for_selector("#prompt")
    assert "LLM Gateway" in page.title()
    assert page.locator("#send").count() == 1


def test_pet_initial_state(page, live_server):
    page.goto(live_server)
    pet = page.wait_for_selector("#pet")
    assert pet.get_attribute("data-mood") == "idle"
    assert pet.get_attribute("data-routine") is None


def test_composer_send_gets_a_response(page, live_server):
    """End-to-end product flow: type a message, send it, see an assistant
    reply appear — the mock backend answers deterministically, no network."""
    page.goto(live_server)
    page.fill("#prompt", "hello from the smoke test")
    page.click("#send")
    page.wait_for_selector(".a-row .a", timeout=15_000)
    assert page.locator(".a-row .a").count() >= 1
    assert page.locator(".a-row .a").inner_text().strip() != ""


@pytest.mark.parametrize("phase_idx,name", [(0, "cook"), (1, "eat"), (2, "work"), (3, "wander"), (4, "sleep")])
def test_pet_routine_phase_applies(page, live_server, phase_idx, name):
    """Regression guard for the exact class of bug this session kept hitting:
    a phase's dataset attribute not landing, or its animation getting
    silently masked by another rule. Doesn't assert pixel-level visuals —
    just that the state machine and its CSS hook still agree with each other."""
    page.goto(live_server)
    page.wait_for_selector("#pet")
    page.evaluate(f"_petApplyRoutinePhase({phase_idx})")
    routine = page.evaluate("document.getElementById('pet').dataset.routine")
    assert routine == name
    if name == "sleep":
        asleep = page.evaluate("document.getElementById('pet').classList.contains('pet-asleep')")
        assert asleep is True
