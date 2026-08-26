"""Code mode's system preamble, including screenshot/design-to-code
guidance for attached images."""
from fastapi.testclient import TestClient

from app.main import CODE_PREAMBLE, _apply_code_mode
from app.main import app

client = TestClient(app)


def test_code_preamble_mentions_image_handling():
    """The preamble is what actually steers the model when a screenshot is
    attached — if this guidance regresses, "build this from the image" quietly
    goes back to being ignored."""
    low = CODE_PREAMBLE.lower()
    assert "image" in low
    assert "design" in low or "screenshot" in low or "mockup" in low


def test_apply_code_mode_prepends_preamble_only_when_on():
    hist = [{"role": "user", "content": "hi"}]
    on = _apply_code_mode(hist, True)
    off = _apply_code_mode(hist, False)
    assert on[0] == {"role": "system", "content": CODE_PREAMBLE}
    assert on[1:] == hist
    assert off == hist   # untouched, not even a copy with an empty system turn


def test_generate_with_code_mode_and_image_succeeds():
    """code_mode + an attached image shouldn't fight each other — the vision
    fallback in _resolve and the code-first preamble are independent knobs."""
    img = "data:image/png;base64,AAAA"
    r = client.post("/generate", json={
        "prompt": "Build this UI", "images": [img], "code_mode": True,
    })
    assert r.status_code == 200
    body = r.json()
    assert "image" in body["text"].lower()   # mock backend echoes what it received
