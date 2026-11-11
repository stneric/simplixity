"""Model discovery must never leave the picker empty or the app broken.

The rule under test throughout: a provider being unreachable is *information*,
not a failure.
"""

from __future__ import annotations

import httpx
import pytest
from fakeredis import aioredis

from app.providers import catalog


@pytest.fixture
async def redis():
    client = aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def openrouter_only(monkeypatch):
    monkeypatch.setattr(catalog.settings, "local_discovery", False)
    monkeypatch.setattr(catalog.settings, "openrouter_api_key", "test-key")
    # .env may hold a real key; the suite must not depend on it.
    monkeypatch.setattr(catalog.settings, "groq_api_key", None)


# Bound before any patching, so the factory below cannot recurse into itself.
REAL_ASYNC_CLIENT = httpx.AsyncClient


def mock_transport(routes: dict[str, object]):
    """Map a URL substring to a JSON body; anything unmatched is unreachable."""

    def handler(request: httpx.Request) -> httpx.Response:
        for fragment, body in routes.items():
            if fragment in str(request.url):
                return httpx.Response(200, json=body)
        raise httpx.ConnectError("unreachable", request=request)

    return httpx.MockTransport(handler)


def patch_http(monkeypatch, routes: dict[str, object], on_request=None):
    """Route every AsyncClient in the app through a mock transport."""

    def handler(request: httpx.Request) -> httpx.Response:
        if on_request:
            on_request(request)
        return mock_transport(routes).handler(request)

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


OPENROUTER_MODELS = {
    "data": [
        {
            "id": "vendor/paid",
            "name": "Paid",
            "context_length": 8192,
            "pricing": {"prompt": "0.0000002", "completion": "0.0000004"},
        },
        {
            "id": "vendor/gratis:free",
            "name": "Gratis (free)",
            "context_length": 32768,
            "pricing": {"prompt": "0", "completion": "0"},
        },
    ]
}


async def test_free_models_are_identified_by_price_not_by_name(monkeypatch, openrouter_only):
    patch_http(monkeypatch, {"openrouter": OPENROUTER_MODELS})
    models, problems = await catalog.discover(catalog.CHAT)

    free = [m for m in models if m.free]
    assert [m.id for m in free] == ["vendor/gratis:free"]
    assert problems == []


async def test_group_puts_free_and_local_in_their_own_buckets():
    models = [
        catalog.ModelInfo(
            "a/paid", "Paid", catalog.OPENROUTER, catalog.CHAT, free=False, local=False
        ),
        catalog.ModelInfo(
            "a/gratis", "Gratis", catalog.OPENROUTER, catalog.CHAT, free=True, local=False
        ),
        catalog.ModelInfo("llama", "llama", catalog.OLLAMA, catalog.CHAT, free=True, local=True),
    ]
    groups = catalog.group(models)

    assert [m["id"] for m in groups["free"]] == ["a/gratis"], "local models are not 'free tier'"
    assert [m["id"] for m in groups["local"]] == ["llama"]
    assert len(groups["all"]) == 3
    assert groups["all"][0]["id"] == "llama", "local sorts first"


async def test_unreachable_provider_yields_a_problem_not_an_exception(monkeypatch):
    monkeypatch.setattr(catalog.settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(catalog.settings, "groq_api_key", None)
    monkeypatch.setattr(catalog.settings, "local_discovery", True)
    patch_http(monkeypatch, {"openrouter": OPENROUTER_MODELS})

    models, problems = await catalog.discover(catalog.CHAT)

    assert len(models) == 2, "openrouter results survive a dead local runtime"
    assert {p["provider"] for p in problems} == {catalog.OLLAMA, catalog.LMSTUDIO}
    assert all(p["error"] == "Not reachable" for p in problems)


async def test_missing_api_key_still_lists_models_and_says_why(monkeypatch):
    monkeypatch.setattr(catalog.settings, "openrouter_api_key", None)
    monkeypatch.setattr(catalog.settings, "groq_api_key", None)
    monkeypatch.setattr(catalog.settings, "local_discovery", False)
    patch_http(monkeypatch, {"openrouter": OPENROUTER_MODELS})

    models, problems = await catalog.discover(catalog.CHAT)

    assert len(models) == 2, "an empty picker teaches the user nothing"
    assert "No API key" in problems[0]["error"]


GROQ_MODELS = {
    "data": [
        {"id": "llama-3.3-70b-versatile", "owned_by": "Meta", "context_window": 131072},
        {"id": "openai/gpt-oss-120b", "owned_by": "OpenAI", "context_window": 131072},
        # Speech models: listed by the API, unusable for chat completions.
        {"id": "whisper-large-v3", "owned_by": "OpenAI", "context_window": 448},
        {"id": "canopylabs/orpheus-v1-english", "owned_by": "Canopy Labs"},
    ]
}


async def test_groq_models_are_discovered_with_their_own_key(monkeypatch):
    monkeypatch.setattr(catalog.settings, "local_discovery", False)
    monkeypatch.setattr(catalog.settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(catalog.settings, "groq_api_key", "gsk-test")
    seen = {}

    def capture(request):
        seen[str(request.url)] = request.headers.get("Authorization")

    patch_http(
        monkeypatch,
        {"openrouter": OPENROUTER_MODELS, "groq": GROQ_MODELS},
        on_request=capture,
    )

    models, problems = await catalog.discover(catalog.CHAT)

    groq = [m for m in models if m.provider == catalog.GROQ]
    assert [m.id for m in groq] == [
        "llama-3.3-70b-versatile",
        "openai/gpt-oss-120b",
    ], "speech models must not reach the chat picker"
    assert all(m.free and m.context_length == 131072 for m in groq)
    assert problems == []
    # Each provider must see its own key, never the other's.
    assert seen["https://api.groq.com/openai/v1/models"] == "Bearer gsk-test"
    assert seen["https://openrouter.ai/api/v1/models"] == "Bearer test-key"


async def test_groq_is_not_probed_without_a_key(monkeypatch, openrouter_only):
    patch_http(monkeypatch, {"openrouter": OPENROUTER_MODELS})
    models, problems = await catalog.discover(catalog.CHAT)

    assert all(m.provider != catalog.GROQ for m in models)
    assert all(p["provider"] != catalog.GROQ for p in problems)


async def test_groq_has_no_embedding_models(monkeypatch):
    monkeypatch.setattr(catalog.settings, "local_discovery", False)
    monkeypatch.setattr(catalog.settings, "openrouter_api_key", None)
    monkeypatch.setattr(catalog.settings, "groq_api_key", "gsk-test")
    patch_http(monkeypatch, {"openrouter": {"data": []}, "groq": GROQ_MODELS})

    models, _ = await catalog.discover(catalog.EMBEDDING)

    assert all(m.provider != catalog.GROQ for m in models)


async def test_results_are_cached_then_refreshable(monkeypatch, redis, openrouter_only):
    calls = 0

    def count(_request):
        nonlocal calls
        calls += 1

    patch_http(monkeypatch, {"openrouter": OPENROUTER_MODELS}, on_request=count)

    await catalog.list_models(redis, catalog.CHAT)
    await catalog.list_models(redis, catalog.CHAT)
    assert calls == 1, "second call should be served from cache"

    await catalog.list_models(redis, catalog.CHAT, refresh=True)
    assert calls == 2, "refresh must bypass the cache"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("nomic-embed-text", True),
        ("mxbai-embed-large", True),
        ("bge-m3", True),
        ("snowflake-arctic-embed2", True),
        ("llama3.2", False),
        ("qwen3.5:latest", False),
        ("gemma4:e4b", False),
    ],
)
def test_embedding_name_heuristic(name, expected):
    """Only used when a local runtime does not declare capabilities."""
    assert catalog.looks_like_embedding(name) is expected


def test_model_key_survives_ids_containing_colons_and_slashes():
    model = catalog.ModelInfo(
        "nvidia/nemotron-3-embed-1b:free",
        "n",
        catalog.OPENROUTER,
        catalog.EMBEDDING,
        free=True,
        local=False,
    )
    provider, _, model_id = model.key.partition("::")
    assert provider == catalog.OPENROUTER
    assert model_id == "nvidia/nemotron-3-embed-1b:free"


# --------------------------------------------------------------- cache windows


def local(name: str = "llama") -> catalog.ModelInfo:
    return catalog.ModelInfo(name, name, catalog.OLLAMA, catalog.CHAT, free=True, local=True)


def remote(name: str = "a/paid") -> catalog.ModelInfo:
    return catalog.ModelInfo(name, name, catalog.OPENROUTER, catalog.CHAT, free=False, local=False)


def test_a_catalogue_without_local_models_is_only_believed_briefly(monkeypatch):
    """The bug this closes: the TTL was picked by whether local models were
    *found*, so a container that started before `ollama serve` cached its
    "ollama: Not reachable" for the full hour. Ollama coming up seconds later
    changed nothing the picker could see, which reads as the app having lost
    local models altogether."""
    monkeypatch.setattr(catalog.settings, "local_discovery", True)

    assert catalog._ttl_for([remote()]) == catalog.LOCAL_RETRY_TTL_S


def test_a_catalogue_with_local_models_keeps_the_ordinary_short_window(monkeypatch):
    monkeypatch.setattr(catalog.settings, "local_discovery", True)

    ttl = catalog._ttl_for([remote(), local()])

    assert ttl == catalog.settings.local_model_cache_ttl_s
    assert ttl > catalog.LOCAL_RETRY_TTL_S, "a found model is not a retry window"


def test_an_install_not_looking_for_local_runtimes_caches_for_the_hour(monkeypatch):
    """Nothing is being waited for, so re-probing the remote catalogue every
    thirty seconds would buy nothing and cost a request per picker open."""
    monkeypatch.setattr(catalog.settings, "local_discovery", False)

    assert catalog._ttl_for([remote()]) == catalog.settings.model_cache_ttl_s


async def test_vision_is_read_from_what_a_model_takes(monkeypatch, openrouter_only, redis):
    patch_http(
        monkeypatch,
        {
            "openrouter": {
                "data": [
                    {"id": "v/sees", "architecture": {"input_modalities": ["text", "image"]}},
                    {"id": "v/blind", "architecture": {"input_modalities": ["text"]}},
                    {"id": "v/unsaid"},
                ]
            }
        },
    )
    models, _ = await catalog.discover(catalog.CHAT)
    assert {m.id: m.vision for m in models} == {"v/sees": True, "v/blind": False, "v/unsaid": None}

    await catalog.list_models(redis, catalog.CHAT)
    assert await catalog.sees(redis, catalog.OPENROUTER, "v/sees") is True
    assert await catalog.sees(redis, catalog.OPENROUTER, "v/blind") is False
    # Not in the catalogue: unknown, which never refuses anything.
    assert await catalog.sees(redis, catalog.OPENROUTER, "v/gone") is None


async def test_ollama_says_vision_in_its_capabilities(monkeypatch):
    monkeypatch.setattr(catalog.settings, "ollama_base_url", "http://ollama.test")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "llava"}, {"name": "llama3"}]})
        name = __import__("json").loads(request.content)["model"]
        caps = ["completion", "vision"] if name == "llava" else ["completion"]
        return httpx.Response(200, json={"capabilities": caps})

    async with REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as client:
        models = await catalog._ollama(client, catalog.CHAT)
    assert {m.id: m.vision for m in models} == {"llava": True, "llama3": False}


# ------------------------------------------------------------ context window


async def test_the_window_is_the_models_own_num_ctx(monkeypatch):
    """A model that brings its window in its Modelfile is fitted to that, not
    to the install-wide default: an 8k writer must not be cut to 4k."""
    monkeypatch.setattr(catalog.settings, "ollama_base_url", "http://ollama.test")
    monkeypatch.setattr(catalog.settings, "local_context_tokens", 4096)
    show = {"parameters": "draft_num_predict              2\nnum_ctx                        8192"}
    patch_http(monkeypatch, {"/api/show": show})

    assert await catalog.context_window(catalog.OLLAMA, "simplixity-writer") == 8192


async def test_without_num_ctx_a_loaded_model_says_what_it_was_given(monkeypatch):
    """OLLAMA_CONTEXT_LENGTH is invisible from here, but a model it loaded is not."""
    monkeypatch.setattr(catalog.settings, "ollama_base_url", "http://ollama.test")
    monkeypatch.setattr(catalog.settings, "local_context_tokens", 4096)
    ps = {"models": [{"name": "qwen3:latest", "model": "qwen3:latest", "context_length": 32768}]}
    patch_http(monkeypatch, {"/api/show": {"parameters": "temperature 0.6"}, "/api/ps": ps})

    assert await catalog.context_window(catalog.OLLAMA, "qwen3") == 32768
    assert await catalog.context_window(catalog.OLLAMA, "other:7b") == 4096


async def test_an_unreachable_runtime_falls_back_to_the_setting(monkeypatch):
    monkeypatch.setattr(catalog.settings, "ollama_base_url", "http://ollama.test")
    monkeypatch.setattr(catalog.settings, "local_context_tokens", 6000)
    patch_http(monkeypatch, {})

    assert await catalog.context_window(catalog.OLLAMA, "anything") == 6000
    assert await catalog.context_window(catalog.LMSTUDIO, "anything") == 6000
    assert await catalog.context_window(catalog.OPENROUTER, "vendor/model") == 6000


async def test_lm_studio_names_the_length_a_loaded_model_was_given(monkeypatch):
    monkeypatch.setattr(catalog.settings, "lmstudio_base_url", "http://lms.test")
    monkeypatch.setattr(catalog.settings, "local_context_tokens", 4096)
    body = {
        "id": "m",
        "state": "loaded",
        "max_context_length": 131072,
        "loaded_context_length": 16384,
    }
    patch_http(monkeypatch, {"/api/v0/models/m": body})

    assert await catalog.context_window(catalog.LMSTUDIO, "m") == 16384
