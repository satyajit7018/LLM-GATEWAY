"""Exact-cache + request-signature tests."""
from app.cache import Cache, request_signature


def test_memory_cache_roundtrip():
    c = Cache()
    assert c.get("sig-a") is None
    c.set("sig-a", {"text": "hi", "tokens": 1, "model": "m"})
    assert c.get("sig-a")["text"] == "hi"


def test_clear_empties_cache():
    c = Cache()
    c.set("k", {"text": "x", "tokens": 1, "model": "m"})
    c.clear()
    assert c.get("k") is None


def test_signature_distinguishes_attachments_and_images():
    base = request_signature("hello")
    with_file = request_signature("hello", attachments=[{"name": "a.txt", "text": "data"}])
    with_img = request_signature("hello", images=["data:image/png;base64,AAAA"])
    assert base != with_file != with_img != base


def test_signature_stable_for_identical_input():
    a = request_signature("q", images=["img1"], attachments=[{"name": "f", "text": "t"}])
    b = request_signature("q", images=["img1"], attachments=[{"name": "f", "text": "t"}])
    assert a == b
