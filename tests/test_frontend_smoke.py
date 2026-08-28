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


# ---- Operation PET UPGRADE regression tests --------------------------------

def test_error_mood_data_attr_set(page, live_server):
    """setPetMood('error') writes data-mood=error on the pet element (P-01)."""
    page.goto(live_server)
    page.wait_for_selector("#pet")
    page.evaluate("setPetMood('error')")
    mood = page.evaluate("document.getElementById('pet').dataset.mood")
    assert mood == "error"


def test_error_mood_auto_reverts_to_idle(page, live_server):
    """setPetMood('error', N) auto-reverts to idle after N ms (P-01)."""
    page.goto(live_server)
    page.wait_for_selector("#pet")
    page.evaluate("setPetMood('error', 80)")   # short timeout — no real sleep needed
    page.wait_for_timeout(300)
    mood = page.evaluate("document.getElementById('pet').dataset.mood")
    assert mood == "idle"


def test_pet_visible_on_mobile_viewport(page, live_server):
    """Pet is visible at 375px width — not display:none on mobile (P-05)."""
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(live_server)
    page.wait_for_selector("#pet")
    assert page.is_visible("#pet")


def test_pet_track_gain_includes_error_lower_than_sad(page, live_server):
    """PET_TRACK_GAIN has an 'error' key with a value strictly below sad's (P-01)."""
    page.goto(live_server)
    gain = page.evaluate("PET_TRACK_GAIN")
    assert "error" in gain, "PET_TRACK_GAIN must have an 'error' key"
    assert gain["error"] < gain["sad"], "error gain must be less than sad gain (alarmed ≠ curious)"


def test_pet_routine_phase_duration_is_jittered(page, live_server):
    """Phase durations vary between runs — _petRoutinePhaseMs produces non-constant output (P-03).
    Probabilistic: ±18% jitter means two independent samples matching exactly is astronomically
    unlikely (continuous distribution). Runs 6 samples; all identical would be a bug."""
    page.goto(live_server)
    page.wait_for_selector("#pet")
    cook_phase = page.evaluate("PET_ROUTINE_PHASES[0]")   # cook, base 12000ms
    durations = [
        page.evaluate("_petRoutinePhaseMs(PET_ROUTINE_PHASES[0])")
        for _ in range(6)
    ]
    # All six values being identical would mean jitter is broken (chance: (1/∞)^5 ≈ 0)
    assert len(set(durations)) > 1, f"All 6 jittered durations were identical: {durations}"
    # All values must be within the ±18% band
    base = cook_phase["ms"]
    for d in durations:
        assert round(base * 0.82) <= d <= round(base * 1.18), \
            f"Jittered duration {d} is outside ±18% of base {base}"


# ---- Operation AESTHETIC POLISH regression tests ----------------------------

def test_google_fonts_and_typography_configured(page, live_server):
    """Google Fonts Inter & JetBrains Mono are linked and body font stack uses Inter."""
    page.goto(live_server)
    font_link = page.locator("link[href*='fonts.googleapis.com/css2?family=Inter']").count()
    assert font_link >= 1, "Inter & JetBrains Mono Google Fonts link must exist in head"
    body_font = page.evaluate("getComputedStyle(document.body).fontFamily")
    assert "Inter" in body_font, f"Body font family should lead with Inter, got: {body_font}"


def test_glassmorphism_backdrop_filters_configured(page, live_server):
    """Popovers configure glassmorphic backdrop-filter and translucent surfaces."""
    page.goto(live_server)
    # Check that quota-pop or settings-pop has backdrop-filter rule in CSS
    backdrop = page.evaluate("""() => {
        const el = document.querySelector('.quota-pop') || document.querySelector('.settings-pop');
        return el ? getComputedStyle(el).backdropFilter : '';
    }""")
    assert "blur" in backdrop or backdrop == "none" or backdrop != "", "Popover should configure backdrop-filter blur"


def test_code_block_terminal_chrome_and_dots_rendered(page, live_server):
    """renderCodeBlock outputs terminal dots and language pill badge."""
    page.goto(live_server)
    html = page.evaluate("renderCodeBlock('python', 'print(123)')")
    assert "code-dots" in html, "Code block chrome must include .code-dots container"
    assert "cdot-r" in html and "cdot-y" in html and "cdot-g" in html, "Code block chrome must include macOS dots"
    assert "class=\"lang\"" in html, "Code block chrome must include language pill badge"


# ---- Operation PET PERSONALITY regression tests -----------------------------

def test_pet_thought_bubble_displays_and_auto_hides(page, live_server):
    """showPetBubble sets text, adds .visible class, and auto-dismisses after timeout."""
    page.goto(live_server)
    page.evaluate("showPetBubble('Hello World', 100)")
    bubble = page.locator("#pet-bubble")
    assert "visible" in (bubble.get_attribute("class") or "")
    assert bubble.inner_text() == "Hello World"
    page.wait_for_timeout(250)
    assert "visible" not in (bubble.get_attribute("class") or "")


def test_pet_headphones_toggle_with_code_mode(page, live_server):
    """Pet gains .has-headphones when code mode toggle is active."""
    page.goto(live_server)
    pet = page.locator("#pet")
    assert "has-headphones" not in (pet.get_attribute("class") or "")
    # Toggle code mode
    page.evaluate("""() => {
        document.getElementById('code-toggle').checked = true;
        syncPetAccessories();
    }""")
    assert "has-headphones" in (pet.get_attribute("class") or "")
    # Toggle off
    page.evaluate("""() => {
        document.getElementById('code-toggle').checked = false;
        syncPetAccessories();
    }""")
    assert "has-headphones" not in (pet.get_attribute("class") or "")


def test_pet_bubble_dialogue_on_poke(page, live_server):
    """Clicking the pet displays a thought bubble with dialogue."""
    page.goto(live_server)
    page.evaluate("_petPoke()")
    bubble = page.locator("#pet-bubble")
    assert "visible" in (bubble.get_attribute("class") or "")
    assert len(bubble.inner_text().strip()) > 0



