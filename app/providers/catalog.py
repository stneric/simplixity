"""Live model discovery across OpenRouter and local runtimes.

The UI needs a real list, not an env var, so this queries each provider and
normalises the results into one shape. Every probe fails soft: an unreachable
Ollama must cost a timeout and a warning, never a broken model picker.

Results are cached in Redis: remote models change slowly (1h), local ones
change whenever the user pulls a model (5m).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

import httpx
from redis.asyncio import Redis

from app.config import settings
from app.llm.client import GROQ, LMSTUDIO, OLLAMA, OPENROUTER

log = logging.getLogger(__name__)

CHAT = "chat"
EMBEDDING = "embedding"

# How long a catalogue with no local models in it is believed while local
# discovery is on. Short, because the thing being waited for is a local process
# on the same machine: starting Ollama after the stack should cost a picker
# reopen, not an hour. Long enough that opening the picker repeatedly does not
# re-probe the remote catalogue on every keystroke.
LOCAL_RETRY_TTL_S = 30

# Ollama and LM Studio do not always declare what a model is for. Names are the
# fallback signal, and embedding models are named remarkably consistently.
_EMBED_HINTS = (
    "embed",
    "embedding",
    "bge",
    "gte",
    "e5-",
    "minilm",
    "mxbai",
    "nomic",
    "arctic-embed",
    "stella",
    "qwen3-embedding",
)

# Groq lists transcription (whisper) and text-to-speech (orpheus, playai-tts)
# models alongside chat models; they cannot serve /chat/completions.
_GROQ_SPEECH = ("whisper", "orpheus", "tts")


@dataclass(slots=True)
class ModelInfo:
    id: str
    name: str
    provider: str
    kind: str
    free: bool
    local: bool
    context_length: int | None = None
    prompt_price: float | None = None
    # Whether the model takes pictures. None is "the provider does not say"
    # (Groq, LM Studio's bare endpoint): such a model is sent the picture and
    # left to answer or refuse, rather than refused here on a guess.
    vision: bool | None = None

    @property
    def key(self) -> str:
        """Stable identity for persistence. Model ids contain ':' (…:free) and
        '/' (vendor/model), so the separator has to be something neither uses."""
        return f"{self.provider}::{self.id}"


def looks_like_embedding(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _EMBED_HINTS)


# --------------------------------------------------------------- openrouter


async def _openrouter(client: httpx.AsyncClient, kind: str) -> list[ModelInfo]:
    # Chat models come back from the bare endpoint; embeddings are a separate
    # output modality and are *not* included in the default listing.
    params = {"output_modalities": "embeddings"} if kind == EMBEDDING else None
    # The model list is public; the key is only attached when one is configured.
    headers = {}
    if settings.openrouter_api_key:
        headers["Authorization"] = f"Bearer {settings.openrouter_api_key}"
    resp = await client.get(
        f"{settings.openrouter_base_url}/models", params=params, headers=headers
    )
    resp.raise_for_status()

    models = []
    for raw in resp.json().get("data", []):
        pricing = raw.get("pricing") or {}
        prompt_price = _as_float(pricing.get("prompt"))
        completion_price = _as_float(pricing.get("completion"))
        modalities = (raw.get("architecture") or {}).get("input_modalities")
        models.append(
            ModelInfo(
                id=raw["id"],
                name=raw.get("name") or raw["id"],
                provider=OPENROUTER,
                kind=kind,
                free=prompt_price == 0 and (completion_price or 0) == 0,
                local=False,
                context_length=raw.get("context_length"),
                prompt_price=prompt_price,
                vision="image" in modalities if isinstance(modalities, list) else None,
            )
        )
    return models


async def _groq(client: httpx.AsyncClient, kind: str) -> list[ModelInfo]:
    # Groq hosts no embedding models, so there is nothing to discover.
    if kind == EMBEDDING:
        return []
    resp = await client.get(
        f"{settings.groq_base_url}/models",
        headers={"Authorization": f"Bearer {settings.groq_api_key}"},
    )
    resp.raise_for_status()

    # Groq's answer carries no name or pricing, only id, owner and context
    # window. The standard tier bills nothing per token, so everything is free.
    # Speech models are listed too but reject chat completions outright; the
    # id is the only signal the API gives to keep them out of the picker.
    return [
        ModelInfo(
            id=raw["id"],
            name=raw["id"],
            provider=GROQ,
            kind=kind,
            free=True,
            local=False,
            context_length=raw.get("context_window"),
            prompt_price=0.0,
        )
        for raw in resp.json().get("data", [])
        if raw.get("id") and not any(hint in raw["id"].lower() for hint in _GROQ_SPEECH)
    ]


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# -------------------------------------------------------------------- local


async def _ollama(client: httpx.AsyncClient, kind: str) -> list[ModelInfo]:
    resp = await client.get(f"{settings.ollama_base_url}/api/tags")
    resp.raise_for_status()
    tags = resp.json().get("models", [])

    async def classify(entry: dict) -> ModelInfo | None:
        name = entry.get("name") or entry.get("model")
        if not name:
            return None
        model_kind, vision = await _ollama_kind(client, name)
        if model_kind != kind:
            return None
        # Parameter size is the one detail worth surfacing for a local model.
        size = (entry.get("details") or {}).get("parameter_size")
        return ModelInfo(
            id=name,
            name=f"{name} ({size})" if size else name,
            provider=OLLAMA,
            kind=kind,
            free=True,  # local inference costs nothing per token
            local=True,
            context_length=entry.get("context_length"),
            prompt_price=0.0,
            vision=vision,
        )

    results = await asyncio.gather(*(classify(entry) for entry in tags), return_exceptions=True)
    return [r for r in results if isinstance(r, ModelInfo)]


async def _ollama_kind(client: httpx.AsyncClient, name: str) -> tuple[str, bool | None]:
    """Ask Ollama what the model can do: its kind, and whether it sees. Falls
    back to the name, which says nothing about vision."""
    try:
        resp = await client.post(f"{settings.ollama_base_url}/api/show", json={"model": name})
        resp.raise_for_status()
        capabilities = resp.json().get("capabilities") or []
        if "embedding" in capabilities:
            return EMBEDDING, False
        if capabilities:
            return CHAT, "vision" in capabilities
    except Exception:
        log.debug("ollama /api/show unavailable for %s, using name heuristic", name)
    return (EMBEDDING, False) if looks_like_embedding(name) else (CHAT, None)


async def _lmstudio(client: httpx.AsyncClient, kind: str) -> list[ModelInfo]:
    """LM Studio's native endpoint declares model type; the OpenAI-compatible
    one does not, so try the richer route first."""
    try:
        resp = await client.get(f"{settings.lmstudio_base_url}/api/v0/models")
        resp.raise_for_status()
        entries = resp.json().get("data", [])
        typed = True
    except Exception:
        resp = await client.get(f"{settings.lmstudio_base_url}/v1/models")
        resp.raise_for_status()
        entries = resp.json().get("data", [])
        typed = False

    models = []
    for raw in entries:
        model_id = raw.get("id")
        if not model_id:
            continue
        vision: bool | None = None
        if typed:
            declared = (raw.get("type") or "").lower()
            model_kind = EMBEDDING if declared == "embeddings" else CHAT
            # "vlm" is LM Studio's word for a model that takes pictures.
            vision = declared == "vlm"
        else:
            model_kind = EMBEDDING if looks_like_embedding(model_id) else CHAT
        if model_kind != kind:
            continue
        models.append(
            ModelInfo(
                id=model_id,
                name=model_id,
                provider=LMSTUDIO,
                kind=kind,
                free=True,
                local=True,
                context_length=raw.get("max_context_length"),
                prompt_price=0.0,
                vision=vision,
            )
        )
    return models


async def context_window(provider: str, model: str) -> int:
    """How many tokens this local model will actually read.

    The runtime decides, per model, and truncates silently past it, so the
    prompt is fitted to the runtime's number rather than to a guess. Ollama
    answers with the model's own `num_ctx` (its Modelfile, which a request
    through the OpenAI endpoint cannot override) or, for a model without one
    that is loaded, the window it was loaded with. LM Studio names the length a
    loaded model was given. Anything else, an unloaded model with no setting of
    its own or a runtime that is not answering, gets `local_context_tokens`,
    which is what the runtime itself falls back to.
    """
    fallback = settings.local_context_tokens
    try:
        async with httpx.AsyncClient(timeout=settings.discovery_timeout_s) as client:
            if provider == OLLAMA:
                found = await _ollama_window(client, model)
            elif provider == LMSTUDIO:
                found = await _lmstudio_window(client, model)
            else:
                return fallback
    except Exception as exc:  # a guess, never a failed run
        log.debug("context window of %s/%s unknown: %s", provider, model, exc)
        return fallback
    return found if isinstance(found, int) and found > 0 else fallback


async def window_of(llm: Any) -> int:
    """The window a client's model is served with: asked of its runtime for a
    local one, 0 for a remote one, whose window is far larger than anything
    this app assembles and is therefore never budgeted against."""
    if not llm.is_local:
        return 0
    return await context_window(getattr(llm, "provider", ""), getattr(llm, "model", ""))


async def _ollama_window(client: httpx.AsyncClient, model: str) -> int | None:
    resp = await client.post(f"{settings.ollama_base_url}/api/show", json={"model": model})
    resp.raise_for_status()
    # "num_ctx                        8192", one parameter per line.
    for line in (resp.json().get("parameters") or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "num_ctx" and parts[1].isdigit():
            return int(parts[1])
    resp = await client.get(f"{settings.ollama_base_url}/api/ps")
    resp.raise_for_status()
    wanted = model if ":" in model else f"{model}:latest"
    for entry in resp.json().get("models", []):
        if wanted in (entry.get("name"), entry.get("model")):
            return entry.get("context_length")
    return None


async def _lmstudio_window(client: httpx.AsyncClient, model: str) -> int | None:
    resp = await client.get(f"{settings.lmstudio_base_url}/api/v0/models/{model}")
    resp.raise_for_status()
    entry = resp.json()
    return entry.get("loaded_context_length") if entry.get("state") == "loaded" else None


# ------------------------------------------------------------------- public


async def discover(kind: str) -> tuple[list[ModelInfo], list[dict[str, str]]]:
    """Probe every configured provider. Returns (models, problems)."""
    # OpenRouter's model list is a public endpoint, so it is queried even
    # without a key: an empty picker teaches the user nothing, whereas a full
    # list plus "no API key configured" tells them exactly what to do next.
    # Groq's endpoint requires auth, so without a key there is nothing to probe.
    sources: list[tuple[str, Any]] = [(OPENROUTER, _openrouter)]
    if settings.groq_api_key:
        sources.append((GROQ, _groq))
    if settings.local_discovery:
        sources.append((OLLAMA, _ollama))
        sources.append((LMSTUDIO, _lmstudio))

    async with httpx.AsyncClient(timeout=settings.discovery_timeout_s) as client:
        results = await asyncio.gather(
            *(fn(client, kind) for _name, fn in sources), return_exceptions=True
        )

    models: list[ModelInfo] = []
    problems: list[dict[str, str]] = []
    for (provider, _fn), result in zip(sources, results, strict=True):
        if isinstance(result, BaseException):
            # A local runtime that simply is not running is the common case and
            # must not be loud.
            log.info("model discovery: %s unavailable (%s)", provider, type(result).__name__)
            problems.append({"provider": provider, "error": _describe(result)})
        else:
            models.extend(result)

    if not settings.openrouter_api_key and any(not m.local for m in models):
        problems.append(
            {
                "provider": OPENROUTER,
                "error": "No API key set. Remote models are listed but cannot run",
            }
        )
    return models, problems


def _describe(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code in (401, 403):
            return "Rejected the API key"
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "Not reachable"
    if isinstance(exc, httpx.TimeoutException):
        return "Timed out"
    return type(exc).__name__


def group(models: Iterable[ModelInfo]) -> dict[str, list[dict]]:
    """Free first: that is the tab most people want to land on."""
    ordered = sorted(models, key=lambda m: (not m.local, m.name.lower()))
    payload = [asdict(m) | {"key": m.key} for m in ordered]
    return {
        "free": [m for m in payload if m["free"] and not m["local"]],
        "local": [m for m in payload if m["local"]],
        "all": payload,
    }


async def list_models(redis: Redis, kind: str, refresh: bool = False) -> dict[str, Any]:
    cache_key = f"models:{kind}"
    if not refresh:
        cached = await redis.get(cache_key)
        if cached:
            return json.loads(cached)

    models, problems = await discover(kind)
    payload = {"kind": kind, "groups": group(models), "problems": problems}

    if models:
        await redis.set(cache_key, json.dumps(payload), ex=_ttl_for(models))
    return payload


async def sees(redis: Redis, provider: str, model: str) -> bool | None:
    """Whether this chat model takes pictures: True, False, or None when the
    provider does not say or the model is not in the catalogue. Read from the
    cached catalogue, so asking costs a Redis read on every run but the first."""
    try:
        payload = await list_models(redis, CHAT)
    except Exception as exc:  # an unknown answer, never a failed run
        log.info("could not read the model catalogue for vision: %s", exc)
        return None
    for entry in payload.get("groups", {}).get("all", []):
        if entry.get("provider") == provider and entry.get("id") == model:
            return entry.get("vision")
    return None


def _ttl_for(models: list[ModelInfo]) -> int:
    """How long this catalogue may be believed.

    The case that most needs a short TTL is the one where local discovery found
    *nothing*. A container started before `ollama serve` discovers remote models
    only, and with a long TTL that result, with its "ollama: Not reachable",
    would be cached for the full hour. Ollama could come up seconds later and the
    picker would go on
    showing an empty Local tab for the rest of the hour, which reads exactly
    like the app having lost the ability to see local models at all.

    So: found local models, the ordinary short TTL, a freshly pulled model still
    appears within minutes. Found none while local discovery is switched on, a
    retry window, because the host may simply not be up yet. Only an install
    that is not looking for local runtimes at all gets the long one.
    """
    if any(m.local for m in models):
        return settings.local_model_cache_ttl_s
    if settings.local_discovery:
        return LOCAL_RETRY_TTL_S
    return settings.model_cache_ttl_s
