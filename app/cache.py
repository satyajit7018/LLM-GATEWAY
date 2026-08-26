"""Exact-match cache (Step 3).

Uses Redis if reachable; otherwise transparently falls back to an in-process
dict so the app runs with no external services. The cache key is the SHA256
of the prompt, so identical prompts hit the cache regardless of length.
"""
import hashlib
import json
import threading
import time
from collections import OrderedDict

from . import config


def cache_key(key_material: str) -> str:
    return "gen:" + hashlib.sha256(key_material.encode()).hexdigest()


def request_signature(prompt: str, images=None, attachments=None, model=None,
                      history=None) -> str:
    """Canonical string identifying a request, so identical prompt+attachments+
    images+model+history hit the cache while any difference (a new image, edited
    file, a different model, more conversation context) misses. Images are hashed
    to keep the key small."""
    parts = ["model:" + (model or ""), prompt or ""]
    for h in history or []:
        parts.append("h:" + h.get("role", "") + ":" + h.get("content", ""))
    for a in attachments or []:
        parts.append("file:" + a["name"] + ":" + a["text"])
    for url in images or []:
        parts.append("img:" + hashlib.sha256(url.encode()).hexdigest())
    return "\x00".join(parts)


class _MemoryBackend:
    """Minimal TTL dict, used when Redis is unavailable.

    Bounded by `max_entries`: once full, the oldest key is FIFO-evicted on each
    insert so memory can't grow without limit (Redis, when present, handles its
    own eviction). Expired entries are also dropped lazily on access.
    """

    name = "memory"

    def __init__(self, max_entries: int = None):
        self._store: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
        self._max = max_entries if max_entries is not None else config.CACHE_MAX_ENTRIES
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            item = self._store.get(key)
            if not item:
                return None
            expires_at, value = item
            if expires_at and expires_at < time.time():
                self._store.pop(key, None)
                return None
            return value

    def setex(self, key: str, ttl: int, value: str):
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (time.time() + ttl if ttl else 0, value)
            while self._max and len(self._store) > self._max:
                self._store.popitem(last=False)  # evict oldest

    def clear(self):
        with self._lock:
            self._store.clear()


class _RedisBackend:
    name = "redis"

    def __init__(self, client):
        self._client = client

    def get(self, key: str):
        return self._client.get(key)

    def setex(self, key: str, ttl: int, value: str):
        self._client.setex(key, ttl, value)

    def clear(self):
        # Only our namespaced keys, so a shared Redis isn't wiped wholesale.
        for k in self._client.scan_iter("gen:*"):
            self._client.delete(k)


def _make_backend():
    try:
        import redis  # imported lazily so the dep is optional at runtime

        client = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
        client.ping()
        return _RedisBackend(client)
    except Exception:
        return _MemoryBackend()


class Cache:
    def __init__(self):
        self._backend = _make_backend()

    @property
    def backend_name(self) -> str:
        return self._backend.name

    def get(self, key_material: str):
        raw = self._backend.get(cache_key(key_material))
        return json.loads(raw) if raw else None

    def set(self, key_material: str, value: dict):
        self._backend.setex(
            cache_key(key_material), config.CACHE_TTL_S, json.dumps(value)
        )

    def clear(self):
        """Empty the cache in place, keeping whichever backend is active
        (used by the load test and /admin/reset). Flushing rather than
        swapping means a Redis-backed cache stays on Redis."""
        self._backend.clear()
