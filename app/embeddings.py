"""Embedding backends for semantic caching (Step 4).

Two backends, selected by config.EMBED_BACKEND:

- "lexical": offline, no downloads. Hashes tokens into a fixed-dim vector with
             TF weighting. Catches near-duplicate prompts that share words
             ("What's the capital of France?" vs "capital of France?"). This is
             the "plain cosine similarity at small scale" option from the plan.
- "sbert":   real sentence-transformers/all-MiniLM-L6-v2 embeddings, which also
             catch paraphrases that share no words. Requires:
                 pip install sentence-transformers

Both return an L2-normalized numpy vector, so cosine similarity is a dot product.
"""
import hashlib
import re

import numpy as np

from . import config

_WORD_RE = re.compile(r"[a-z0-9]+")
_sbert_model = None


def _stable_bucket(tok: str, dim: int) -> int:
    """Map a token to a bucket deterministically across processes.

    Python's built-in hash() is randomized per process (PYTHONHASHSEED), which
    would make the same prompt embed to different vectors on each server
    restart — so which prompts count as near-duplicates would drift, and tests
    that depend on two tokens *not* colliding become flaky. A fixed digest
    keeps bucketing reproducible.
    """
    h = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(h, "big") % dim


# Common function / instruction words. Dropping them lets the distinctive
# content words dominate the lexical vector, so "capital of Italy" and
# "capital of Spain" no longer look near-identical (they differ only by the
# country, which is exactly the token that matters).
_STOP = frozenset((
    "a an the is are am was were be been being of to in on at for and or but "
    "what whats which who whom how why when where do does did can could would "
    "should will shall may might must me my mine you your yours i we our us it "
    "its this that these those there here as with by from about into over under "
    "please give show tell list name explain describe summarize one word words "
    "answer sentence short brief detailed simple just some any"
).split())


def _tokenize(text: str):
    toks = _WORD_RE.findall(text.lower())
    content = [t for t in toks if t not in _STOP]
    return content or toks  # fall back to all tokens if everything was filtered


def _lexical_embed(text: str) -> np.ndarray:
    vec = np.zeros(config.EMBED_DIM, dtype=np.float32)
    for tok in _tokenize(text):
        idx = _stable_bucket(tok, config.EMBED_DIM)
        vec[idx] += 1.0
    return vec


def _sbert_embed(text: str) -> np.ndarray:
    global _sbert_model
    if _sbert_model is None:
        from sentence_transformers import SentenceTransformer  # lazy import

        _sbert_model = SentenceTransformer(config.EMBED_MODEL)
    return np.asarray(_sbert_model.encode(text), dtype=np.float32)


def embed(text: str) -> np.ndarray:
    """Return an L2-normalized embedding vector for `text`."""
    vec = _sbert_embed(text) if config.EMBED_BACKEND == "sbert" else _lexical_embed(text)
    norm = np.linalg.norm(vec)
    return vec / norm if norm else vec


def backend_name() -> str:
    return config.EMBED_BACKEND
