from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Env-driven configuration. Field names map case-insensitively to env vars,
    so `redis_url` is set by REDIS_URL.

    Only OPENROUTER_API_KEY is genuinely required; everything else has a
    default that works both in the compose stack and on a laptop.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- infrastructure ---
    redis_url: str = "redis://localhost:6379"
    database_path: str = "./data/simplixity.db"

    # --- search ---
    searxng_api_url: str = "http://localhost:8080"
    # "auto" lets SearXNG infer the language from the query, so a German question
    # is not searched as if it were English. The answer is written in the user's
    # language either way, and the sources should match it.
    search_language: str = "auto"
    search_timeout_s: float = 15.0
    # SearXNG proxies public engines that rate-limit; a failed search must
    # degrade the answer, never fail the run.
    search_concurrency: int = 4

    # Page content. SearXNG returns snippets written for humans, so anything
    # beyond a shallow answer needs the actual page.
    jina_reader_url: str = "https://r.jina.ai"
    jina_api_key: str | None = None
    content_timeout_s: float = 25.0
    content_concurrency: int = 4
    max_content_chars: int = 24000

    # --- llm + embeddings ---
    # One OpenRouter key serves both; the LLM layer is OpenAI-wire-compatible.
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # OpenRouter attributes requests to these; they are optional but make usage
    # legible in the dashboard.
    app_url: str = "https://github.com/stneric/simplixity"
    app_title: str = "Simplixity"

    # Groq. Optional second remote provider; the base URL already ends in /v1.
    groq_api_key: str | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"

    # Local inference. Both speak the OpenAI wire format, so they need no extra
    # client, only a different base_url. Discovery probes them and fails soft,
    # so an unreachable endpoint costs a timeout, never an error.
    ollama_base_url: str = "http://localhost:11434"
    lmstudio_base_url: str = "http://localhost:1234"
    local_discovery: bool = True
    discovery_timeout_s: float = 3.0
    model_cache_ttl_s: int = 3600
    local_model_cache_ttl_s: int = 300

    # Which models run is chosen in the UI and stored per profile
    # (`store.settings_store.UISettings`), never here: a second place to set
    # them would only be a default the UI silently overrides.
    default_depth: str = "auto"
    # Naming a new chat with a model. Off by default, and deliberately: the call
    # happens *after* the run's footer has been written, so whatever it costs can
    # never appear in the price the run reports, and a truncated question is a
    # perfectly good name for a chat. Measured at ~$0.0005 per new chat on
    # Gemini Flash, almost all of it reasoning tokens spent on a six-word title.
    title_enabled: bool = False
    # Where "what is the weather" means when the question does not say. Not
    # geolocation: the CSP and the permissions policy exclude that,
    # so this is the only place a default place can come from.
    home_location: str = ""
    # Comma-separated lens ids (see app/search/lenses.py): web, scholar, news,
    # docs, reference. The UI's persisted choice wins over this.
    default_source_lenses: str = "web"

    # --- literature (app/scholar, the literature page) ---
    # The paper databases are queried directly rather than through SearXNG:
    # SearXNG cuts an abstract to a snippet and hands back no stable ids, and
    # both are what screening and "more like these, not like those" run on.
    # IEEE Xplore answers only with a key; without one its papers still arrive
    # through OpenAlex and Semantic Scholar, by DOI, often without the abstract.
    ieee_api_key: str | None = None
    # Optional. Without it Semantic Scholar shares one rate-limited pool with
    # every keyless caller on the internet, and a 429 there is routine.
    semantic_scholar_api_key: str | None = None
    # Optional. OpenAlex meters by key; keyless calls get the smaller allowance.
    openalex_api_key: str | None = None
    # A contact address for the OpenAlex and Crossref "polite pool", which is
    # faster and more reliable than the anonymous one. Empty sends none: an
    # address is the operator's to give, never something this app fills in.
    scholar_mailto: str = ""
    scholar_timeout_s: float = 20.0

    # Free tiers rate-limit per *request*, so send many inputs per call.
    embedding_batch_size: int = 64
    embedding_timeout_s: float = 30.0
    # How far a literature project's best passage must stand above its median
    # one before the semantic search counts any hit (`search.library`). Tune
    # it per install with tools/calibrate_library.py; a narrow corpus may want less.
    library_semantic_gap: float = 0.12
    # Passages a run embeds itself before searching, when their stored vectors
    # are missing or stale (a model change, a new INDEX_VERSION). Bounded so a
    # first question after a change cannot embed a whole library in one go, and
    # small enough (eight requests) that a free tier's rate limit lets it through.
    library_reembed_limit: int = 512

    # --- llm budgets ---
    llm_timeout_s: float = 120.0
    llm_stream_timeout_s: float = 300.0
    llm_concurrency: int = 4
    llm_max_retries: int = 2
    # Room for a thinking model to think, on top of the answer budget every
    # call site asks for. The provider's `max_tokens` covers both, and without
    # this a reasoning model spends the whole ceiling before writing a word.
    # Half the call's own budget, never more than this. 0 turns it off.
    reasoning_max_tokens: int = 1024

    # --- run budgets ---
    run_ttl_seconds: int = 86400
    max_concurrent_runs: int = 8
    # What a local runtime actually serves, in tokens. Ollama defaults to 4096
    # and truncates anything longer **silently**, returning HTTP 200 with the
    # overflow gone: a deep run sent 11 500 tokens, the model saw 4 096, and the
    # part that fell away included the numbered source list. It then wrote a
    # report citing "[Quelle impliziert durch den Kontext]", which is not a
    # model being stupid but a model being asked to cite a list it never saw.
    #
    # The app fits its prompt to the window instead of finding out afterwards,
    # and asks the runtime for it per model (`catalog.context_window`): Ollama's
    # `num_ctx` from the model's Modelfile, or the window a loaded model has.
    # This number is only the fallback, for a model that has neither yet; it is
    # what Ollama serves by default, so set it to `OLLAMA_CONTEXT_LENGTH` when
    # the runtime sets that.
    local_context_tokens: int = 4096
    # Depth presets carry their own wall clock (see agent/depth.py); this is the
    # ceiling none of them may exceed.
    max_wall_clock_s: float = 900.0

    # --- server ---
    # 3000 because that is what the reverse proxy in front of this expects.
    port: int = 3000
    # A single-user app behind Cloudflare/Traefik still wants a stop on runaway
    # clients: a stuck reload loop can otherwise burn an API budget overnight.
    rate_limit_runs_per_minute: int = 20
    # Which peers may set X-Forwarded-For/-Proto, comma-separated addresses or
    # networks. "*" trusts everyone, which is right only while nothing but the
    # proxy can reach the app; with the port published, any client can claim
    # any address. Set it to the proxy's network (e.g. the Docker network
    # Traefik sits on, `172.18.0.0/16`) once the port is reachable directly.
    forwarded_allow_ips: str = "*"

    # --- misc ---
    log_level: str = "INFO"

    @property
    def default_lenses(self) -> tuple[str, ...]:
        return tuple(part.strip() for part in self.default_source_lenses.split(",") if part.strip())


settings = Settings()
