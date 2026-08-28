import threading
import time

import numpy as np

from . import config
from .embeddings import backend_name, embed


class SemanticCache:
    def __init__(self, persist: bool = False):
        self._vectors: list[np.ndarray] = []
        self._responses: list[dict] = []
        self._prompts: list[str] = []
        self._last_accessed: list[float] = []
        self._matrix = None  # cached np.vstack, rebuilt only when entries change
        self._persist = persist
        self._lock = threading.Lock()

        if self._persist:
            self._load_persisted()


    def _load_persisted(self):
        try:
            from . import store
            entries = store.load_all_semantic_entries()
            for prompt, resp in entries:
                vec = embed(prompt)
                self._vectors.append(vec)
                self._responses.append(resp)
                self._prompts.append(prompt)
                self._last_accessed.append(time.time())
        except Exception:
            pass

    @property
    def backend_name(self) -> str:
        return backend_name()

    @property
    def size(self) -> int:
        return len(self._prompts)

    def lookup(self, prompt: str):
        """Return (response, matched_prompt, score) or None."""
        vec = embed(prompt)
        with self._lock:
            if not self._vectors:
                return None
            if self._matrix is None:              # rebuild only after a change
                self._matrix = np.vstack(self._vectors)
            matrix = self._matrix
            responses, prompts = self._responses, self._prompts
            sims = matrix @ vec  # cosine, since all rows + vec are normalized
            best = int(np.argmax(sims))
            score = float(sims[best])
            if score >= config.SEMANTIC_THRESHOLD:
                self._last_accessed[best] = time.time()  # LRU touch on hit
                return responses[best], prompts[best], score
        return None

    def clear(self):
        with self._lock:
            self._vectors.clear()
            self._responses.clear()
            self._prompts.clear()
            self._last_accessed.clear()
            self._matrix = None
            if self._persist:
                try:
                    from . import store
                    store.clear_semantic_entries()
                except Exception:
                    pass

    def add(self, prompt: str, response: dict):
        vec = embed(prompt)
        now = time.time()
        with self._lock:
            if len(self._prompts) >= config.SEMANTIC_MAX_ENTRIES:
                # LRU eviction: evict least-recently accessed entry to preserve hot items.
                lru_idx = int(np.argmin(self._last_accessed)) if self._last_accessed else 0
                self._vectors.pop(lru_idx)
                self._responses.pop(lru_idx)
                self._prompts.pop(lru_idx)
                self._last_accessed.pop(lru_idx)
            self._vectors.append(vec)
            self._responses.append(response)
            self._prompts.append(prompt)
            self._last_accessed.append(now)
            self._matrix = None  # invalidate cached matrix

            if self._persist:
                try:
                    from . import store
                    store.save_semantic_entry(prompt, response)
                except Exception:
                    pass


