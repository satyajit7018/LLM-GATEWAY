"""Semantic cache tests (lexical embedder)."""
import pytest

from app import store
from app.semantic_cache import SemanticCache


@pytest.fixture(autouse=True)
def _clean_store():
    store.clear_semantic_entries()
    yield
    store.clear_semantic_entries()


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


def test_semantic_cache_persistence_across_instances():
    """Entries added to SemanticCache persist and are loaded by a new SemanticCache instance."""
    sc1 = SemanticCache(persist=True)
    sc1.clear()
    sc1.add("What is machine learning?", _resp("AI subset"))
    assert sc1.size == 1

    # New instance simulates a fresh server restart
    sc2 = SemanticCache(persist=True)
    assert sc2.size == 1
    hit = sc2.lookup("machine learning?")
    assert hit is not None
    assert hit[0]["text"] == "AI subset"
    sc2.clear()


