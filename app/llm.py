"""LLM backend abstraction.

Two backends, selected by config.LLM_BACKEND:

- "mock":  deterministic, offline, no API key. Sleeps for MOCK_LATENCY_S to
           imitate real network/inference latency so caching wins are visible.
- "groq"/"openai": real OpenAI-compatible chat/completions call via httpx.

Requests may carry:
- attachments: [{name, text}]  -> extracted text file contents, folded into the
                                  prompt as context (uses the text model).
- images:      [data-url, ...] -> routed to the vision model (config.LLM_VISION_MODEL).
"""
import hashlib
import json
import random
import re
import time

import threading

import httpx

from . import config

# Shared, connection-pooled client — reuses keep-alive TCP/TLS connections
# across requests instead of a fresh handshake per call.
_client = httpx.Client(
    timeout=httpx.Timeout(90.0, connect=10.0),
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=50,
                        keepalive_expiry=30.0),
)


def close_client():
    """Release pooled connections. Call once on server shutdown."""
    _client.close()


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

# Latest rate-limit / quota snapshot per provider, captured from response
# headers. Keyed by provider name ("groq", "gemini", …) so switching models
# shows the quota for *that* provider, not whichever was hit last.
_last_quota = {}
_quota_lock = threading.Lock()


def last_quota(provider: str = None) -> dict:
    """Snapshot for one provider (or the most recent overall if None)."""
    with _quota_lock:
        if provider is not None:
            return dict(_last_quota.get(provider, {}))
        latest = max(_last_quota.values(), key=lambda s: s.get("updated", 0),
                     default={})
        return dict(latest)


class LLMError(RuntimeError):
    pass


def _strip_reasoning(text: str) -> str:
    """Remove <think>…</think> blocks emitted by reasoning models (e.g. Qwen)."""
    cleaned = _THINK_RE.sub("", text).strip()
    return cleaned or text.strip()


def _capture_quota(headers, provider):
    """Store OpenAI/Groq-style rate-limit headers, keyed by provider."""
    def g(k):
        return headers.get(k)
    snap = {
        "provider": provider,
        "limit_tokens": g("x-ratelimit-limit-tokens"),
        "remaining_tokens": g("x-ratelimit-remaining-tokens"),
        "reset_tokens": g("x-ratelimit-reset-tokens"),
        "limit_requests": g("x-ratelimit-limit-requests"),
        "remaining_requests": g("x-ratelimit-remaining-requests"),
        "reset_requests": g("x-ratelimit-reset-requests"),
        "updated": time.time(),
    }
    if any(v is not None for k, v in snap.items()
           if k not in ("updated", "provider")):
        with _quota_lock:
            _last_quota[provider] = snap


def _compose_prompt(prompt: str, attachments) -> str:
    """Fold text-file attachments into the prompt as context."""
    if not attachments:
        return prompt
    parts = ["The user attached the following file(s):\n"]
    for a in attachments:
        parts.append(f"--- {a['name']} ---\n{a['text']}\n")
    parts.append(f"\nUsing the file(s) above, answer:\n{prompt}")
    return "\n".join(parts)


def _mock_generate(prompt, images, attachments) -> dict:
    time.sleep(config.MOCK_LATENCY_S)
    if config.MOCK_FAIL_RATE and random.random() < config.MOCK_FAIL_RATE:
        raise LLMError("mock: injected transient failure")
    extras = []
    if images:
        extras.append(f"{len(images)} image(s)")
    if attachments:
        extras.append(f"{len(attachments)} file(s): " +
                      ", ".join(a["name"] for a in attachments))
    note = f" [received {', '.join(extras)}]" if extras else ""
    model = config.LLM_VISION_MODEL if images else config.LLM_MODEL
    digest = hashlib.sha256((prompt or "").encode()).hexdigest()[:8]
    text = (f"[mock:{model}]{note} You asked: {(prompt or '').strip()[:200]!r}. "
            f"Deterministic stub response (id={digest}).")
    tokens = max(1, (len(prompt or "") + len(text)) // 4)
    return {"text": text, "tokens": tokens, "model": model}


def _build_messages(composed, images, history=None):
    msgs = [{"role": h["role"], "content": h["content"]} for h in (history or [])]
    if images:
        content = [{"type": "text", "text": composed or "Describe the image(s)."}]
        for url in images:
            content.append({"type": "image_url", "image_url": {"url": url}})
        msgs.append({"role": "user", "content": content})
    else:
        msgs.append({"role": "user", "content": composed})
    return msgs


def _resolve(model_id, has_images, key_override=None):
    """Pick a catalog entry and its provider connection details. If the request
    has images but the chosen model can't see, fall back to the vision model.
    `key_override` (a user's own bring-your-own key) takes priority over the
    app-owned key from the environment when present."""
    entry = config.MODEL_BY_ID.get(model_id) or config.MODEL_BY_ID[config.DEFAULT_MODEL_ID]
    if has_images and not entry.get("vision"):
        entry = config.MODEL_BY_ID.get(config.VISION_MODEL_ID, entry)
    provider = entry["provider"]
    spec = config.PROVIDERS[provider]
    return {"provider": provider, "base_url": spec["base_url"],
            "key": key_override or config.provider_key(provider), "key_env": spec["key_env"],
            "model": entry["model"], "byo": bool(key_override)}


def _api_generate(prompt, images, attachments, r, history=None) -> dict:
    if not r["key"]:
        raise LLMError(f"provider '{r['provider']}' has no API key — set {r['key_env']}")
    composed = _compose_prompt(prompt, attachments)
    messages = _build_messages(composed, images, history)
    headers = {"Authorization": f"Bearer {r['key']}"}
    payload = {"model": r["model"], "messages": messages}
    try:
        resp = _client.post(r["base_url"], headers=headers, json=payload)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise LLMError(f"LLM API call failed: {exc}") from exc
    if not r["byo"]:   # a user's own key's quota is theirs, not the shared app pool's
        _capture_quota(resp.headers, r["provider"])
    data = resp.json()
    text = _strip_reasoning(data["choices"][0]["message"]["content"])
    usage = data.get("usage", {})
    tokens = usage.get("total_tokens", max(1, (len(composed) + len(text)) // 4))
    return {"text": text, "tokens": tokens, "model": r["model"]}


def validate_key(provider: str, api_key: str) -> tuple:
    """Make one cheap real call to confirm a BYO key actually works, so a typo
    or an expired/revoked key gets caught at save time instead of silently
    failing later on first real use. Returns (valid, error_message).

    Deliberately conservative: only an explicit 401/403 counts as "bad key".
    Anything else (rate limit, timeout, provider hiccup) is *not* proof the
    key is wrong, so we let it through rather than block a good key on a
    transient failure.
    """
    if config.LLM_BACKEND == "mock":   # offline dev/tests — nothing to call
        return True, None
    spec = config.PROVIDERS.get(provider)
    if not spec:
        return False, f"unknown provider '{provider}'"
    entry = next((m for m in config.MODEL_CATALOG if m["provider"] == provider), None)
    if not entry:
        return True, None   # no catalog model to test against — don't block
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {"model": entry["model"], "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
    try:
        resp = _client.post(spec["base_url"], headers=headers, json=payload, timeout=15.0)
    except httpx.HTTPError:
        return True, None
    if resp.status_code in (401, 403):
        return False, "That key was rejected by the provider — check it's correct and active."
    return True, None


def generate(prompt, images=None, attachments=None, model_id=None, history=None,
            key_override=None) -> dict:
    """Return {text, tokens, model}. `model_id` selects a catalog entry
    (provider + model); None uses config.DEFAULT_MODEL_ID. `history` is prior
    turns [{role, content}] prepended for conversation memory. `key_override`
    is a user's own bring-your-own provider key, used instead of the app's."""
    if config.LLM_BACKEND == "mock":
        return _mock_generate(prompt, images, attachments)
    return _api_generate(prompt, images, attachments,
                         _resolve(model_id, bool(images), key_override), history)


# --- streaming ----------------------------------------------------------
# Generators yield {"delta": str} chunks as text arrives, then one
# {"final": {text, tokens, model}} with the assembled result.

def _mock_stream(prompt, images, attachments):
    result = _mock_generate(prompt, images, attachments)  # includes the sleep
    words = result["text"].split(" ")
    for i, w in enumerate(words):
        time.sleep(0.02)
        yield {"delta": (" " if i else "") + w}
    yield {"final": result}


def _api_stream(prompt, images, attachments, r, history=None):
    if not r["key"]:
        raise LLMError(f"provider '{r['provider']}' has no API key — set {r['key_env']}")
    composed = _compose_prompt(prompt, attachments)
    model = r["model"]
    headers = {"Authorization": f"Bearer {r['key']}"}
    payload = {
        "model": model,
        "messages": _build_messages(composed, images, history),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    acc, tokens, in_think = [], None, False
    try:
        with _client.stream("POST", r["base_url"], headers=headers,
                            json=payload) as resp:
            resp.raise_for_status()
            if not r["byo"]:
                _capture_quota(resp.headers, r["provider"])
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if obj.get("usage"):
                    tokens = obj["usage"].get("total_tokens", tokens)
                for ch in obj.get("choices", []):
                    piece = (ch.get("delta") or {}).get("content")
                    if not piece:
                        continue
                    acc.append(piece)
                    # Suppress reasoning models' <think>…</think> spans live.
                    if "<think>" in piece:
                        in_think = True
                    if in_think:
                        if "</think>" in piece:
                            in_think = False
                        continue
                    yield {"delta": piece}
    except httpx.HTTPError as exc:
        raise LLMError(f"LLM stream failed: {exc}") from exc
    text = _strip_reasoning("".join(acc))
    if tokens is None:
        tokens = max(1, (len(composed) + len(text)) // 4)
    yield {"final": {"text": text, "tokens": tokens, "model": model}}


def generate_stream(prompt, images=None, attachments=None, model_id=None, history=None,
                    key_override=None):
    if config.LLM_BACKEND == "mock":
        yield from _mock_stream(prompt, images, attachments)
        return
    r = _resolve(model_id, bool(images), key_override)
    # Providers whose SSE is slow/flaky (e.g. Gemini) skip streaming and do a
    # single fast non-streaming call, surfaced as one chunk.
    if config.PROVIDERS.get(r["provider"], {}).get("stream", True):
        yield from _api_stream(prompt, images, attachments, r, history)
    else:
        result = _api_generate(prompt, images, attachments, r, history)
        yield {"delta": result["text"]}
        yield {"final": result}
