"""Semantic cache tests (lexical embedder)."""
from app.semantic_cache import SemanticCache


def _resp(text="Paris"):
    return {"text": text, "tokens": 1, "model": "m"}


def test_near_duplicate_hits():
    sc = SemanticCache()
    sc.add("What is the capital of France?", _resp())
    match = sc.lookup("capital of France?")
    assert match is not None
    _payload, _prompt, score = match
    assert score >= 0.65


def test_unrelated_prompt_misses():
    sc = SemanticCache()
    sc.add("What is the capital of France?", _resp())
    assert sc.lookup("How do I bake sourdough bread?") is None


def test_entity_swapped_question_misses():
    # Regression: "capital of Spain" must NOT return the cached "capital of
    # Italy" answer just because the sentences share function words.
    sc = SemanticCache()
    sc.add("What is the capital of Italy? one word", _resp("Rome"))
    assert sc.lookup("What is the capital of Spain? one word") is None


def test_clear_resets_store():
    sc = SemanticCache()
    sc.add("prompt one", _resp())
    assert sc.size == 1
    sc.clear()
    assert sc.size == 0
    assert sc.lookup("prompt one") is None


def test_lru_eviction_preserves_frequently_accessed_items(monkeypatch):
    """When cache exceeds capacity, the least recently accessed item is evicted."""
    from app import config
    monkeypatch.setattr(config, "SEMANTIC_MAX_ENTRIES", 2)
    sc = SemanticCache()
    sc.add("item 1", _resp("ans1"))
    sc.add("item 2", _resp("ans2"))

    # Access item 1 to refresh its LRU timestamp
    sc.lookup("item 1")

    # Adding item 3 should evict item 2 (the least recently used), keeping item 1
    sc.add("item 3", _resp("ans3"))
    assert sc.size == 2
    assert sc.lookup("item 1") is not None
    assert sc.lookup("item 3") is not None
    assert sc.lookup("item 2") is None

