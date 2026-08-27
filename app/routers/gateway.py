"""The core LLM gateway: cached + resilient generation (sync and SSE
streaming), the model catalog, quota/usage views, and server-side
conversation sync.

This is the one router that touches the shared cache/semantic-cache/stats
singletons in state.py on nearly every request — everything else in the app
(auth, workspace, admin) is comparatively self-contained.
"""
import json
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import config, metrics, reqlog, store
from ..files import extract_pdf_text
from ..llm import LLMError, generate, generate_stream, last_quota
from ..resilience import CircuitOpenError, breaker, bucket, per_user_bucket, resilient_generate
from ..cache import request_signature
from ..state import _bump, _cache, _cost, _semantic, current_user, require_real_user, require_user

router = APIRouter()

# Injected as a system turn when a request has code_mode on. Steers the model
# to a code-first answer, which the UI then syntax-highlights / previews.
CODE_PREAMBLE = (
    "You are a coding assistant. Prioritize correct, runnable code. Lead your "
    "answer with the code inside a fenced block whose info string is the "
    "language (```python, ```js, ```html, …). Keep prose short and put it after "
    "the code. If the request is a web UI or something visual, return one "
    "self-contained ```html block (inline CSS/JS, no external files) so it can "
    "be previewed directly. If the user attached an image, treat it as a "
    "design reference — a screenshot, mockup, or wireframe of a UI to build or "
    "match — and write HTML/CSS (and JS if needed) that reproduces its layout, "
    "spacing, colors, and text as closely as you can, even if they didn't "
    "spell out every detail in words."
)


def _apply_code_mode(hist: list[dict], code_mode: bool) -> list[dict]:
    """Prepend the code-first system turn when code_mode is on."""
    if code_mode:
        return [{"role": "system", "content": CODE_PREAMBLE}] + hist
    return hist


def _normalize_attachments(attachments) -> list[dict]:
    """Return uniform {name, text} dicts, extracting PDF text server-side."""
    out = []
    for a in attachments:
        if a.pdf_b64:
            try:
                text = extract_pdf_text(a.pdf_b64)
            except Exception as exc:  # malformed/encrypted PDF
                text = f"[could not read PDF {a.name}: {exc}]"
            out.append({"name": a.name, "text": text})
        else:
            out.append({"name": a.name, "text": a.text})
    return out


def _uid(user):
    """This user's id for attributing a request-log row, or None (anonymous /
    AUTH_REQUIRED off) — keeps per-user usage/cost views from mixing accounts."""
    return user["id"] if user else None


def _rate_limit_key(request: Request, user) -> str:
    """Per-account rate-limit key — falls back to client IP when nobody's
    logged in (AUTH_REQUIRED off), so anonymous access still gets *a* per-key
    limit rather than being exempt from the per-user check entirely."""
    if user:
        return f"user:{user['id']}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


def _byo_key_for(user, model_id: str):
    """The logged-in user's own key for this model's provider, if they've
    added one (hybrid build, Phase 2) — None falls back to the app's key."""
    if not user:
        return None
    entry = config.MODEL_BY_ID.get(model_id)
    if not entry:
        return None
    try:
        return store.get_user_key(user["id"], entry["provider"])
    except store.KeysDisabledError:
        return None


def _check_free_quota(user, byo_key):
    """429 if this call would use the app's own key and the user has already
    used up today's free allowance (hybrid build, Phase 3). A no-op for BYO-key
    calls (costs the user, not us), when quota is disabled (limit=0), or when
    there's no user to attribute usage to (AUTH_REQUIRED off)."""
    if byo_key or not user or not config.FREE_DAILY_TOKEN_LIMIT:
        return
    used = store.usage_today(user["id"])["tokens"]
    if used >= config.FREE_DAILY_TOKEN_LIMIT:
        raise HTTPException(status_code=429,
            detail=f"Free daily limit reached ({config.FREE_DAILY_TOKEN_LIMIT} tokens). "
                   "Add your own API key in Settings to keep going, or come back tomorrow.")


def _charge_free_quota(user, byo_key, tokens):
    """Record usage against today's free allowance — only for real (uncached)
    calls that used the app's own key."""
    if byo_key or not user:
        return
    store.record_usage(user["id"], tokens)


class Attachment(BaseModel):
    name: str
    text: str = ""            # text content read client-side (text/code files)
    pdf_b64: Optional[str] = None  # base64 PDF; text extracted server-side


class Turn(BaseModel):
    role: str                 # "user" | "assistant"
    content: str


class GenerateRequest(BaseModel):
    prompt: str = Field(default="", max_length=200_000)
    images: list[str] = Field(default_factory=list)       # data: URLs
    attachments: list[Attachment] = Field(default_factory=list)
    model: Optional[str] = None                           # catalog id; None = default
    history: list[Turn] = Field(default_factory=list)     # prior turns (memory mode)
    code_mode: bool = False                               # code-first assistant persona

    def has_payload(self) -> bool:
        return bool(self.prompt.strip() or self.images or self.attachments)

    def model_id(self) -> str:
        return self.model if self.model in config.MODEL_BY_ID else config.DEFAULT_MODEL_ID


class GenerateResponse(BaseModel):
    text: str
    model: str
    tokens: int
    cached: bool
    cache_type: str  # "none" | "exact" | "semantic"
    latency_ms: float
    match_score: Optional[float] = None


@router.get("/models")
def models(request: Request):
    """Catalog of selectable models. A model is available if the app has a key
    for its provider (the free tier) or, once logged in, if the user has added
    their own key for it (hybrid build, Phase 2) — 'source' says which."""
    user = current_user(request)
    user_providers = set()
    if user:
        try:
            user_providers = {k["provider"] for k in store.list_user_keys(user["id"])}
        except store.KeysDisabledError:
            pass
    out = []
    for m in config.MODEL_CATALOG:
        has_app_key = bool(config.provider_key(m["provider"])) or config.LLM_BACKEND == "mock"
        has_user_key = m["provider"] in user_providers
        out.append({
            "id": m["id"], "label": m["label"], "provider": m["provider"],
            "best_for": m.get("best_for", ""), "vision": m.get("vision", False),
            "available": has_app_key or has_user_key,
            "source": "user" if has_user_key else ("app" if has_app_key else None),
        })
    return {"default": config.DEFAULT_MODEL_ID, "backend": config.LLM_BACKEND,
            "models": out, "keys_enabled": store.keys_enabled()}


@router.get("/quota")
def quota(model: str = None):
    """Latest rate-limit snapshot for the selected model's provider.

    `model` is a catalog id (e.g. "gemini/flash"); its provider decides which
    quota is shown, so switching models switches the numbers. Empty until the
    first real call to that provider; not applicable to the mock backend.
    Some providers (e.g. Gemini) don't return token-quota headers.
    """
    entry = config.MODEL_BY_ID.get(model) or config.MODEL_BY_ID[config.DEFAULT_MODEL_ID]
    provider = entry["provider"]
    q = last_quota(provider)
    return {
        "backend": config.LLM_BACKEND,
        "provider": provider,
        "model": entry["id"],
        "model_label": entry.get("label", entry["id"]),
        "reports_quota": provider == "groq",  # Groq sends x-ratelimit-* headers
        "available": bool(q) and config.LLM_BACKEND != "mock",
        "quota": q,
    }


@router.post("/generate", response_model=GenerateResponse)
def generate_endpoint(req: GenerateRequest, request: Request,
                      cache_mode: str = "default", user=Depends(require_user)):
    start = time.perf_counter()

    if not req.has_payload():
        raise HTTPException(status_code=422, detail="prompt, image or file required")

    # --- rate limit (Step 5) — global cap, then a per-account cap (hybrid build)
    # so one busy account can't 429 every other account by exhausting the
    # shared budget alone. ---
    if not bucket.allow():
        _bump("rate_limited")
        metrics.record_error("rate_limited")
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    if not per_user_bucket.allow(_rate_limit_key(request, user)):
        _bump("rate_limited")
        metrics.record_error("rate_limited")
        raise HTTPException(status_code=429, detail="you're sending requests too fast — slow down a bit")

    atts = _normalize_attachments(req.attachments)
    hist = _apply_code_mode([t.model_dump() for t in req.history], req.code_mode)
    has_extras = bool(req.images or atts or hist)
    model_id = req.model_id()
    sig = request_signature(req.prompt, req.images, atts, model_id, hist)

    use_exact = cache_mode in ("default", "exact", "semantic")
    # Semantic cache only applies to plain-text prompts on the default model with
    # no conversation history: embedding the prompt while ignoring an
    # image/file/model/history would false-hit.
    use_semantic = (
        (config.SEMANTIC_CACHE_ENABLED if cache_mode == "default"
         else cache_mode == "semantic")
        and not has_extras
        and model_id == config.DEFAULT_MODEL_ID
    )

    def _elapsed_ms():
        return round((time.perf_counter() - start) * 1000, 2)

    # --- exact-match cache (Step 3) — keyed on prompt + files + images ---
    if use_exact:
        cached = _cache.get(sig)
        if cached is not None:
            _bump("hits_exact")
            metrics.record_request("hit_exact", time.perf_counter() - start)
            reqlog.log("exact", "exact", cached["model"], cached["tokens"],
                       _elapsed_ms(), 0.0, bool(req.images), bool(atts), user_id=_uid(user))
            return GenerateResponse(**cached, cached=True, cache_type="exact",
                                    latency_ms=_elapsed_ms())

    # --- semantic cache (Step 4) ---
    if use_semantic:
        match = _semantic.lookup(req.prompt)
        if match is not None:
            response, _matched, score = match
            _bump("hits_semantic")
            metrics.record_request("hit_semantic", time.perf_counter() - start)
            reqlog.log("semantic", "semantic", response["model"],
                       response["tokens"], _elapsed_ms(), 0.0, user_id=_uid(user))
            return GenerateResponse(**response, cached=True, cache_type="semantic",
                                    latency_ms=_elapsed_ms(),
                                    match_score=round(score, 4))

    # --- upstream call behind circuit breaker + retry/backoff (Step 5) ---
    byo_key = _byo_key_for(user, model_id)
    _check_free_quota(user, byo_key)   # Phase 3: only bites app-key (free-tier) calls
    try:
        result = resilient_generate(
            lambda _p: generate(req.prompt, images=req.images, attachments=atts,
                                 model_id=model_id, history=hist, key_override=byo_key),
            req.prompt,
        )
    except CircuitOpenError as exc:
        _bump("circuit_open")
        metrics.record_error("circuit_open")
        raise HTTPException(status_code=503, detail=str(exc))
    except LLMError as exc:
        _bump("errors")
        metrics.record_error("upstream")
        raise HTTPException(status_code=502, detail=str(exc))

    if use_exact:
        _cache.set(sig, result)
    if use_semantic:
        _semantic.add(req.prompt, result)
    _charge_free_quota(user, byo_key, result["tokens"])

    _bump("misses")
    metrics.record_request("miss", time.perf_counter() - start)
    # Log the catalog id (not the raw provider model string) — it's the stable,
    # user-facing key the UI's per-model usage view groups by.
    reqlog.log("miss", "none", model_id, result["tokens"], _elapsed_ms(),
               _cost(result["tokens"]), bool(req.images), bool(atts), user_id=_uid(user))
    return GenerateResponse(**result, cached=False, cache_type="none",
                            latency_ms=_elapsed_ms())


@router.get("/log/summary")
def log_summary():
    """Aggregated request log (hit rate, tokens, cost by result)."""
    return reqlog.summary()


# Maps a provider's raw model string (e.g. "openai/gpt-oss-20b") back to its
# catalog id (e.g. "groq/gpt-oss-20b"). reqlog rows written before this
# mapping existed were logged under the raw string; folding them in here
# means old usage stays visible under the model's current catalog id instead
# of sitting orphaned under a key nothing looks up anymore. It also makes a
# future MODEL_CATALOG id rename safe, since the join key is the provider's
# model string, not the id.
_RAW_MODEL_TO_ID = {m["model"]: m["id"] for m in config.MODEL_CATALOG}


@router.get("/log/usage")
def log_usage(user=Depends(require_real_user)):
    """This user's own per-model token/request/cost totals from real
    (non-cached) calls, keyed by catalog id — durable across restarts and
    shared across the user's own browsers/devices. Scoped to the logged-in
    user (hybrid build) — this used to be a whole-server aggregate, which
    mixed every account's usage together once there was more than one."""
    merged: dict = {}
    for key, val in reqlog.usage_by_model(user_id=user["id"]).items():
        target = merged.setdefault(_RAW_MODEL_TO_ID.get(key, key),
                                    {"requests": 0, "tokens": 0, "cost_usd": 0.0})
        target["requests"] += val["requests"]
        target["tokens"] += val["tokens"]
        target["cost_usd"] = round(target["cost_usd"] + val["cost_usd"], 6)
    return {"by_model": merged}


@router.get("/usage/free")
def usage_free(user=Depends(require_real_user)):
    """This user's free-tier (app-owned-key) usage today, and the daily cap —
    hybrid build, Phase 3. BYO-key usage never counts here."""
    limit = config.FREE_DAILY_TOKEN_LIMIT
    used = store.usage_today(user["id"])["tokens"]
    return {"unlimited": limit == 0, "limit": limit, "used": used,
            "remaining": max(0, limit - used) if limit else None}


# --- server-side conversation sync (hybrid build, Phase 4) ----------------
class SyncPush(BaseModel):
    data: str = Field(max_length=10_000_000)   # the frontend's whole chat-store JSON, as a string


@router.get("/sync/store")
def sync_pull(user=Depends(require_real_user)):
    """This user's synced chat history, so it follows them across browsers/
    devices instead of being stuck in one browser's localStorage."""
    return {"data": store.get_user_store(user["id"])}


@router.put("/sync/store")
def sync_push(body: SyncPush, user=Depends(require_real_user)):
    try:
        store.set_user_store(user["id"], body.data)
    except store.UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@router.post("/generate/stream")
def generate_stream_endpoint(req: GenerateRequest, request: Request, cache_mode: str = "default",
                             user=Depends(require_user)):
    """SSE streaming variant of /generate.

    Cache hits are emitted instantly as a single frame; misses stream tokens
    from the provider as they arrive. Rate limiting + circuit breaking still
    apply; mid-stream retries do not (a partial stream can't be replayed), so
    the non-streaming /generate remains the fully-resilient path.
    """
    start = time.perf_counter()
    if not req.has_payload():
        raise HTTPException(status_code=422, detail="prompt, image or file required")
    if not bucket.allow():
        _bump("rate_limited")
        metrics.record_error("rate_limited")
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    if not per_user_bucket.allow(_rate_limit_key(request, user)):
        _bump("rate_limited")
        metrics.record_error("rate_limited")
        raise HTTPException(status_code=429, detail="you're sending requests too fast — slow down a bit")

    atts = _normalize_attachments(req.attachments)
    hist = _apply_code_mode([t.model_dump() for t in req.history], req.code_mode)
    has_extras = bool(req.images or atts or hist)
    model_id = req.model_id()
    byo_key = _byo_key_for(user, model_id)
    _check_free_quota(user, byo_key)   # before any SSE headers are sent, so this is a clean 429
    sig = request_signature(req.prompt, req.images, atts, model_id, hist)
    use_exact = cache_mode in ("default", "exact", "semantic")
    use_semantic = (
        (config.SEMANTIC_CACHE_ENABLED if cache_mode == "default"
         else cache_mode == "semantic") and not has_extras
        and model_id == config.DEFAULT_MODEL_ID
    )

    def frame(obj):
        return f"data: {json.dumps(obj)}\n\n"

    def elapsed_ms():
        return round((time.perf_counter() - start) * 1000, 2)

    def cached_frames(payload, cache_type, score=None):
        meta = {"done": True, "cached": True, "cache_type": cache_type,
                "text": payload["text"], "tokens": payload["tokens"],
                "model": payload["model"], "latency_ms": elapsed_ms(),
                "match_score": score}
        yield frame({"full": payload["text"]})
        yield frame(meta)
        yield "data: [DONE]\n\n"

    def sse():
        # Cache checks first — a hit streams instantly.
        if use_exact:
            hit = _cache.get(sig)
            if hit is not None:
                _bump("hits_exact")
                metrics.record_request("hit_exact", time.perf_counter() - start)
                reqlog.log("exact", "exact", hit["model"], hit["tokens"],
                           elapsed_ms(), 0.0, bool(req.images), bool(atts), True, _uid(user))
                yield from cached_frames(hit, "exact")
                return
        if use_semantic:
            match = _semantic.lookup(req.prompt)
            if match is not None:
                payload, _m, score = match
                _bump("hits_semantic")
                metrics.record_request("hit_semantic", time.perf_counter() - start)
                reqlog.log("semantic", "semantic", payload["model"],
                           payload["tokens"], elapsed_ms(), 0.0, streamed=True, user_id=_uid(user))
                yield from cached_frames(payload, "semantic", round(score, 4))
                return

        # Miss — stream from the provider behind the circuit breaker.
        try:
            breaker.before_call()
        except CircuitOpenError as exc:
            _bump("circuit_open")
            metrics.record_error("circuit_open")
            yield frame({"error": str(exc)})
            return
        result, streamed_any = None, False
        try:
            for ev in generate_stream(req.prompt, images=req.images,
                                      attachments=atts, model_id=model_id, history=hist,
                                      key_override=byo_key):
                if "delta" in ev:
                    streamed_any = True
                    yield frame({"delta": ev["delta"]})
                elif "final" in ev:
                    result = ev["final"]
        except LLMError as exc:
            # Some providers' streaming endpoints are flaky (e.g. Gemini's
            # OpenAI-compat layer 503s intermittently). If nothing streamed yet,
            # fall back to a single non-streaming call so the request succeeds.
            if not streamed_any:
                try:
                    result = generate(req.prompt, images=req.images,
                                      attachments=atts, model_id=model_id, history=hist,
                                      key_override=byo_key)
                    yield frame({"full": result["text"]})
                except LLMError as exc2:
                    breaker.record_failure()
                    _bump("errors")
                    metrics.record_error("upstream")
                    yield frame({"error": str(exc2)})
                    return
            else:
                breaker.record_failure()
                _bump("errors")
                metrics.record_error("upstream")
                yield frame({"error": str(exc)})
                return
        if result is None:  # stream ended without a result frame
            _bump("errors")
            metrics.record_error("upstream")
            yield frame({"error": "empty response from provider"})
            return
        breaker.record_success()

        if use_exact:
            _cache.set(sig, result)
        if use_semantic:
            _semantic.add(req.prompt, result)
        _charge_free_quota(user, byo_key, result["tokens"])
        _bump("misses")
        metrics.record_request("miss", time.perf_counter() - start)
        reqlog.log("miss", "none", model_id, result["tokens"],
                   elapsed_ms(), _cost(result["tokens"]),
                   bool(req.images), bool(atts), True, _uid(user))
        yield frame({"done": True, "cached": False, "cache_type": "none",
                     "tokens": result["tokens"], "model": result["model"],
                     "latency_ms": elapsed_ms(), "match_score": None})
        yield "data: [DONE]\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream")
