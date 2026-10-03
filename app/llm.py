"""OpenAI-compatible chat client with a provider/model fallback chain.

Primary: Gemini Flash-Lite (free tier, ~1-1.5s, solid tool calling, multilingual).
Fallbacks: other Gemini models (separate free quotas), then Groq if a key is set.
"""
import base64
import copy
import logging
import time

import httpx

from . import config

log = logging.getLogger("llm")

GEMINI_OPENAI = "https://generativelanguage.googleapis.com/v1beta/openai"
GEMINI_NATIVE = "https://generativelanguage.googleapis.com/v1beta"
GROQ_OPENAI = "https://api.groq.com/openai/v1"

_client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0))   # slow model → fall through
_cooldown_until: dict[str, float] = {}


class LLMError(Exception):
    pass


def _providers() -> list[dict]:
    chain = []
    if config.GEMINI_API_KEY:
        for m in config.GEMINI_MODELS:
            extra = {"reasoning_effort": config.GEMINI_REASONING} if config.GEMINI_REASONING else {}
            chain.append({"name": f"gemini:{m}", "kind": "gemini", "base": GEMINI_OPENAI,
                          "key": config.GEMINI_API_KEY, "model": m, "extra": extra})
    if config.GROQ_API_KEY:
        for m in config.GROQ_MODELS:
            chain.append({"name": f"groq:{m}", "kind": "groq", "base": GROQ_OPENAI,
                          "key": config.GROQ_API_KEY, "model": m, "extra": {}})
    return chain


def _strip_vendor_fields(messages: list[dict]) -> list[dict]:
    """Gemini attaches thought signatures under tool_calls[].extra_content; other vendors reject them."""
    out = copy.deepcopy(messages)
    for m in out:
        for tc in m.get("tool_calls") or []:
            tc.pop("extra_content", None)
        m.pop("extra_content", None)
    return out


def _retry_delay(r: httpx.Response, default: float) -> float:
    """Gemini's 429 body says how long the per-minute quota needs (RetryInfo.retryDelay = '27s')."""
    try:
        body = r.json()
        err = (body[0] if isinstance(body, list) else body)["error"]
        for d in err.get("details", []):
            if "retryDelay" in d:
                return min(float(d["retryDelay"].rstrip("s")) + 1, 120)
    except Exception:
        pass
    return default


async def chat(messages: list[dict], tools: list[dict] | None = None, temperature: float = 0.3) -> tuple[dict, str]:
    """Returns (assistant_message, provider_name)."""
    providers = _providers()
    if not providers:
        raise LLMError("No LLM API key configured (set GEMINI_API_KEY in .env)")

    errors = []
    for p in providers:
        if _cooldown_until.get(p["name"], 0) > time.time():
            continue
        body = {"model": p["model"], "temperature": temperature, **p["extra"],
                "messages": messages if p["kind"] == "gemini" else _strip_vendor_fields(messages)}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        try:
            t0 = time.time()
            r = await _client.post(f"{p['base']}/chat/completions", json=body,
                                   headers={"Authorization": f"Bearer {p['key']}"})
        except httpx.HTTPError as e:
            errors.append(f"{p['name']}: {e!r}")
            _cooldown_until[p["name"]] = time.time() + 15
            continue

        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                data = data[0]
            log.info("LLM %s %.2fs", p["name"], time.time() - t0)
            return data["choices"][0]["message"], p["name"]

        errors.append(f"{p['name']}: HTTP {r.status_code} {r.text[:200]}")
        log.warning("LLM %s failed: HTTP %s %s", p["name"], r.status_code, r.text[:300])
        if r.status_code == 429:
            _cooldown_until[p["name"]] = time.time() + _retry_delay(r, 30)
        elif r.status_code in (401, 403, 404):
            _cooldown_until[p["name"]] = time.time() + 600   # bad key / model gone: skip for a while
        elif r.status_code >= 500:
            _cooldown_until[p["name"]] = time.time() + 10
        # 400 = our request was rejected by this model; just try the next one

    raise LLMError("; ".join(errors) or "All LLM providers are cooling down")


async def understand_media(data: bytes, mime: str, prompt: str) -> str:
    """Voice notes / photos → text, via Gemini's native multimodal endpoint."""
    if not config.GEMINI_API_KEY:
        raise LLMError("GEMINI_API_KEY required for voice/image understanding")
    mime = mime.split(";")[0].strip()
    body = {"contents": [{"parts": [
        {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}},
        {"text": prompt},
    ]}]}
    errors = []
    for m in config.GEMINI_MODELS:
        r = await _client.post(f"{GEMINI_NATIVE}/models/{m}:generateContent", json=body,
                               headers={"x-goog-api-key": config.GEMINI_API_KEY})
        if r.status_code == 200:
            parts = r.json()["candidates"][0]["content"].get("parts", [])
            return "".join(p.get("text", "") for p in parts).strip()
        errors.append(f"{m}: HTTP {r.status_code} {r.text[:200]}")
    raise LLMError("; ".join(errors))
