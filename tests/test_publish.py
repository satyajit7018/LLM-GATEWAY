"""Publishing a Code-tab project to a stable, self-hosted link (the
"deploy" feature) — no third-party account or API token involved, just a
short slug served back as a standalone page from this same server."""
import uuid

from fastapi.testclient import TestClient

from app.main import app


def _client():
    return TestClient(app)


def test_publish_returns_a_working_url():
    c = _client()
    r = c.post("/publish", json={"html": "<h1>hello world</h1>"})
    assert r.status_code == 200
    d = r.json()
    assert d["slug"] and d["url"].endswith("/p/" + d["slug"])

    page = c.get("/p/" + d["slug"])
    assert page.status_code == 200
    assert "hello world" in page.text
    assert page.headers["content-type"].startswith("text/html")


def test_publish_rejects_empty_html():
    c = _client()
    r = c.post("/publish", json={"html": "   "})
    assert r.status_code == 400


def test_publish_rejects_oversized_html():
    c = _client()
    r = c.post("/publish", json={"html": "<p>" + ("x" * 3_100_000) + "</p>"})
    assert r.status_code == 400


def test_unpublished_slug_404s():
    c = _client()
    r = c.get("/p/does-not-exist")
    assert r.status_code == 404


def test_two_publishes_get_different_slugs_and_dont_collide():
    c = _client()
    a = c.post("/publish", json={"html": "<p>A</p>"}).json()
    b = c.post("/publish", json={"html": "<p>B</p>"}).json()
    assert a["slug"] != b["slug"]
    assert "A" in c.get("/p/" + a["slug"]).text
    assert "B" in c.get("/p/" + b["slug"]).text


def test_published_page_is_not_wrapped_in_the_app_shell():
    c = _client()
    d = c.post("/publish", json={"html": "<p>bare page</p>"}).json()
    page_text = c.get("/p/" + d["slug"]).text
    assert page_text.strip() == "<p>bare page</p>"


def test_publish_records_owning_user_when_logged_in():
    from app import store
    c = _client()
    c.post("/auth/signup", json={"email": f"pub{uuid.uuid4().hex[:12]}@example.com", "password": "hunter2pass"})
    d = c.post("/publish", json={"html": "<p>mine</p>", "conv_id": "conv123"}).json()
    with store._lock:
        row = store._connect().execute(
            "SELECT user_id, conv_id FROM published_pages WHERE slug = ?", (d["slug"],)).fetchone()
    assert row[0] is not None
    assert row[1] == "conv123"
